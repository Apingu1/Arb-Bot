from __future__ import annotations

import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from .discovery import updown_15m_window_from_slug
from .maker_research import (
    MakerRiskStats,
    MarketRegimeTracker,
    QueueAwareVariantEngine,
    QueueLeg,
    SurgeSnapshot,
    VariantCampaign,
    ZERO,
    ONE,
    _utc_now,
)
from .models import MarketPair
from .storage import JsonlRecorder
from .strategy import ArbitrageEngine


class MultiAssetQueueAwareVariantEngine(QueueAwareVariantEngine):
    """Phase 1.7 maker control with generic recurring-market expiry handling."""

    def _seconds_to_expiry(self, slug: str) -> float:
        window = updown_15m_window_from_slug(slug)
        if not window:
            return 999999.0
        _, end = window
        return max(0.0, (end - datetime.now(timezone.utc)).total_seconds())


class PairedMakerVariantEngine(MultiAssetQueueAwareVariantEngine):
    """Selective best-bid complete-set maker control.

    Unlike historical MAKER-99/98/... variants, PMAKER does not manufacture a
    deeper pair by stepping away from the market. It only joins the *current*
    best bids when their combined cost already satisfies the configured pair
    target and both displayed queues are small/balanced enough to be worth the
    queue-risk experiment.
    """

    def __init__(
        self,
        settings,
        recorder: JsonlRecorder,
        regime: MarketRegimeTracker,
        *,
        max_queue: Decimal,
    ) -> None:
        super().__init__(
            settings,
            recorder,
            regime,
            mode="MAKER",
            target_pair=settings.paired_maker_target_pair,
        )
        self.max_queue = max_queue
        self.shares = settings.paired_maker_trade_shares
        self.strategy_name = f"PMAKER-Q{format(max_queue.normalize(), 'f')}"
        # Recreate equity under the final strategy name; historical base maker
        # stats remain independent from these selective controls.
        from .strategy_metrics import StrategyEquity

        self.equity = StrategyEquity(self.strategy_name, recorder)
        self.pair_skips = 0
        self.queue_skips = 0
        self.imbalance_skips = 0

    def _maybe_place(self, engine: ArbitrageEngine, pair: MarketPair, surge: SurgeSnapshot) -> None:
        now = time.monotonic()
        if now < self.cooldown_until.get(pair.market_id, 0.0):
            return
        if surge.active or self._local_toxic(now):
            self.surge_skips += 1
            return
        if self._seconds_to_expiry(pair.slug) <= self.settings.maker_min_seconds_to_expiry:
            return

        a = engine.books.get(pair.token_a)
        b = engine.books.get(pair.token_b)
        if not a or not b or not a.ready or not b.ready:
            return
        bid_a = a.best_bid()
        bid_b = b.best_bid()
        if bid_a is None or bid_b is None:
            return

        combined = bid_a + bid_b
        gross_edge = ONE - combined
        if combined > self.settings.paired_maker_target_pair or gross_edge < self.settings.paired_maker_min_gross_edge_per_share:
            self.pair_skips += 1
            return

        queue_a = a.bids.get(bid_a, ZERO)
        queue_b = b.bids.get(bid_b, ZERO)
        if queue_a <= ZERO or queue_b <= ZERO or queue_a > self.max_queue or queue_b > self.max_queue:
            self.queue_skips += 1
            return

        smaller = min(queue_a, queue_b)
        imbalance = max(queue_a, queue_b) / smaller if smaller > ZERO else Decimal("999999")
        if imbalance > self.settings.paired_maker_max_queue_imbalance:
            self.imbalance_skips += 1
            return

        if (
            self.settings.maker_use_empirical_risk_gate
            and self.risk.filled_campaigns >= self.settings.maker_empirical_risk_min_samples
            and gross_edge <= self.risk.estimated_reserve_per_share
        ):
            self.risk_skips += 1
            return

        campaign = VariantCampaign(
            market_id=pair.market_id,
            slug=pair.slug,
            target_pair=self.settings.paired_maker_target_pair,
            shares=self.shares,
            placed_at=now,
            placed_at_utc=_utc_now(),
            leg_a=QueueLeg(bid_a, queue_a, queue_a, last_trade_seen=a.last_trade_monotonic, last_reprice_at=now),
            leg_b=QueueLeg(bid_b, queue_b, queue_b, last_trade_seen=b.last_trade_monotonic, last_reprice_at=now),
        )
        self.campaigns[pair.market_id] = campaign
        self.placed += 1
        self.total_initial_queue += queue_a + queue_b
        self.recorder.write(
            "maker_variant_campaign_placed",
            {
                "strategy": self.strategy_name,
                "mode": "PAIRED_MAKER",
                "paired_control": True,
                "placed_at": campaign.placed_at_utc,
                "market_id": pair.market_id,
                "slug": pair.slug,
                "target_pair": self.settings.paired_maker_target_pair,
                "shares": self.shares,
                "best_bid_a_at_placement": bid_a,
                "best_bid_b_at_placement": bid_b,
                "maker_bid_a": bid_a,
                "maker_bid_b": bid_b,
                "combined_bid_cost": combined,
                "gross_edge_per_share": gross_edge,
                "initial_queue_ahead_a": queue_a,
                "initial_queue_ahead_b": queue_b,
                "queue_imbalance": imbalance,
                "max_queue_per_leg": self.max_queue,
                "fill_model": "QUEUE_AHEAD_CONSUMED_BY_SELL_VOLUME_OR_TRADE_THROUGH",
            },
        )

    def diagnostic_row(self) -> dict[str, Any]:
        row = super().diagnostic_row()
        row.update(
            {
                "mode": "PAIRED_MAKER",
                "max_queue": self.max_queue,
                "pair_skips": self.pair_skips,
                "queue_skips": self.queue_skips,
                "imbalance_skips": self.imbalance_skips,
            }
        )
        return row


class MakerResearchSuiteV17:
    """Historical maker/hybrid controls plus selective PMAKER queue variants."""

    def __init__(self, settings, recorder: JsonlRecorder) -> None:
        self.settings = settings
        self.recorder = recorder
        self.regime = MarketRegimeTracker(settings)
        self.makers = [
            MultiAssetQueueAwareVariantEngine(settings, recorder, self.regime, mode="MAKER", target_pair=target)
            for target in settings.maker_variant_targets
        ]
        self.hybrids = [
            MultiAssetQueueAwareVariantEngine(settings, recorder, self.regime, mode="HYBRID", target_pair=target)
            for target in settings.maker_variant_targets
        ]
        self.paired_makers = (
            [
                PairedMakerVariantEngine(settings, recorder, self.regime, max_queue=max_queue)
                for max_queue in settings.paired_maker_max_queues
            ]
            if settings.paired_maker_enabled
            else []
        )
        self.variants = [*self.makers, *self.hybrids, *self.paired_makers]

    def on_market_update(self, engine: ArbitrageEngine, market_id: str) -> None:
        surge = self.regime.observe(engine, market_id)
        for variant in self.variants:
            variant.on_market_update(engine, market_id, surge)

    def process_due(self, engine: ArbitrageEngine) -> None:
        for variant in self.variants:
            variant.process_due(engine)

    def diagnostic_rows(self) -> list[dict[str, Any]]:
        return [variant.diagnostic_row() for variant in self.variants]

    @property
    def maker_total_pnl(self) -> Decimal:
        return sum((variant.total_pnl for variant in self.makers), ZERO)

    @property
    def hybrid_total_pnl(self) -> Decimal:
        return sum((variant.total_pnl for variant in self.hybrids), ZERO)

    @property
    def paired_maker_total_pnl(self) -> Decimal:
        return sum((variant.total_pnl for variant in self.paired_makers), ZERO)
