from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from .episode_clustering import outcome_episode_clusterer
from .fees import taker_fee
from .maker_research import (
    MarketRegimeTracker,
    QueueLeg,
    SurgeSnapshot,
    VariantCampaign,
    ZERO,
    ONE,
    _quote_payload,
    _utc_now,
    target_bids,
)
from .maker_research_v17 import MultiAssetQueueAwareVariantEngine, PairedMakerVariantEngine
from .models import MarketPair
from .storage import JsonlRecorder
from .strategy import ArbitrageEngine


def _ratio(a: Decimal, b: Decimal) -> Decimal:
    small = min(a, b)
    if small <= ZERO:
        return Decimal("999999")
    return max(a, b) / small


def _install_campaign(
    variant,
    pair: MarketPair,
    book_a,
    book_b,
    *,
    bid_a: Decimal,
    bid_b: Decimal,
    queue_a: Decimal,
    queue_b: Decimal,
    gross_edge: Decimal,
    extra: dict[str, Any],
) -> None:
    now = time.monotonic()
    campaign = VariantCampaign(
        market_id=pair.market_id,
        slug=pair.slug,
        target_pair=variant.target_pair,
        shares=variant.shares,
        placed_at=now,
        placed_at_utc=_utc_now(),
        leg_a=QueueLeg(
            bid_a,
            queue_a,
            queue_a,
            last_trade_seen=book_a.last_trade_monotonic,
            last_reprice_at=now,
        ),
        leg_b=QueueLeg(
            bid_b,
            queue_b,
            queue_b,
            last_trade_seen=book_b.last_trade_monotonic,
            last_reprice_at=now,
        ),
    )
    variant.campaigns[pair.market_id] = campaign
    variant.placed += 1
    variant.total_initial_queue += queue_a + queue_b
    variant.recorder.write(
        "maker_variant_campaign_placed",
        {
            "strategy": variant.strategy_name,
            "mode": variant.mode,
            "selective_v181": True,
            "placed_at": campaign.placed_at_utc,
            "market_id": pair.market_id,
            "slug": pair.slug,
            "target_pair": variant.target_pair,
            "shares": variant.shares,
            "best_bid_a_at_placement": book_a.best_bid(),
            "best_bid_b_at_placement": book_b.best_bid(),
            "maker_bid_a": bid_a,
            "maker_bid_b": bid_b,
            "combined_bid_cost": bid_a + bid_b,
            "gross_edge_per_share": gross_edge,
            "initial_queue_ahead_a": queue_a,
            "initial_queue_ahead_b": queue_b,
            "queue_imbalance": _ratio(queue_a, queue_b),
            "small_queue": min(queue_a, queue_b),
            "max_queue": max(queue_a, queue_b),
            "fill_model": "QUEUE_AHEAD_CONSUMED_BY_SELL_VOLUME_OR_TRADE_THROUGH",
            **extra,
        },
    )


class FirstFillTelemetryMixin:
    """Attach first-fill microstructure and correlated episode IDs to outcomes."""

    def _telemetry_setup(self) -> None:
        self._first_fill_snapshots: dict[str, dict[str, Any]] = {}

    def _midpoint_move(self, market_id: str, window_s: float) -> Decimal:
        series = list(self.regime.history.get(market_id, ()))
        if not series:
            return ZERO
        now = time.monotonic()
        current = series[-1][1]
        recent = [(ts, px) for ts, px in series if ts >= now - window_s]
        baseline = recent[0][1] if recent else current
        return current - baseline

    def _capture_first_fill_snapshot(
        self,
        engine: ArbitrageEngine,
        pair: MarketPair,
        campaign: VariantCampaign,
    ) -> None:
        if campaign.market_id in self._first_fill_snapshots:
            return

        book_a = engine.books.get(pair.token_a)
        book_b = engine.books.get(pair.token_b)
        if book_a is None or book_b is None:
            return

        a_fill = campaign.leg_a.filled_qty
        b_fill = campaign.leg_b.filled_qty
        if a_fill > ZERO and b_fill <= ZERO:
            first_side = "A"
        elif b_fill > ZERO and a_fill <= ZERO:
            first_side = "B"
        elif a_fill > ZERO and b_fill > ZERO:
            if book_a.last_trade_monotonic and book_b.last_trade_monotonic:
                delta = abs(book_a.last_trade_monotonic - book_b.last_trade_monotonic)
                if delta <= 0.001:
                    first_side = "BOTH_SAME_UPDATE"
                else:
                    first_side = "A" if book_a.last_trade_monotonic < book_b.last_trade_monotonic else "B"
            else:
                first_side = "BOTH_SAME_UPDATE"
        else:
            return

        q_a = campaign.leg_a.initial_queue_ahead
        q_b = campaign.leg_b.initial_queue_ahead
        smaller_queue_leg = "A" if q_a < q_b else "B" if q_b < q_a else "BALANCED"

        exposure_leg, exposure_qty, exposure_avg = self._exposure_cost(campaign)
        opposite_book = None
        if exposure_leg == "A":
            opposite_book = book_b
        elif exposure_leg == "B":
            opposite_book = book_a
        elif first_side == "A":
            opposite_book = book_b
        elif first_side == "B":
            opposite_book = book_a

        one_quote = opposite_book.quote_buy(Decimal("1")) if opposite_book and opposite_book.ready else None
        five_quote = opposite_book.quote_buy(Decimal("5")) if opposite_book and opposite_book.ready else None
        complete_quote = (
            opposite_book.quote_buy(exposure_qty)
            if opposite_book and opposite_book.ready and exposure_qty > ZERO
            else None
        )
        complete_fee = (
            taker_fee(complete_quote.segments, self.settings.crypto_taker_fee_rate)
            if complete_quote is not None
            else ZERO
        )
        complete_net = None
        complete_edge = None
        if complete_quote is not None and exposure_qty > ZERO:
            complete_net = (
                exposure_qty
                - exposure_qty * exposure_avg
                - complete_quote.notional
                - complete_fee
            )
            complete_edge = complete_net / exposure_qty

        opposite_best_ask = opposite_book.best_ask() if opposite_book else None
        surge = self.regime.current(campaign.market_id)
        now = time.monotonic()
        first_fill_ms = (
            Decimal(str((campaign.first_fill_at - campaign.placed_at) * 1000))
            if campaign.first_fill_at is not None
            else None
        )

        snapshot = {
            "captured_at": _utc_now(),
            "first_fill_side": first_side,
            "first_fill_ms": first_fill_ms,
            "maker_pair_at_first_fill": campaign.leg_a.price + campaign.leg_b.price,
            "initial_queue_ahead_a": q_a,
            "initial_queue_ahead_b": q_b,
            "small_queue_initial": min(q_a, q_b),
            "max_queue_initial": max(q_a, q_b),
            "queue_imbalance_initial": _ratio(q_a, q_b),
            "smaller_queue_leg": smaller_queue_leg,
            "first_fill_was_smaller_queue_leg": (
                first_side == smaller_queue_leg if first_side in {"A", "B"} else None
            ),
            "exposure_leg_after_first_update": exposure_leg,
            "exposure_qty_after_first_update": exposure_qty,
            "opposite_best_ask": opposite_best_ask,
            "opposite_depth_at_best_ask": (
                opposite_book.asks.get(opposite_best_ask, ZERO)
                if opposite_book is not None and opposite_best_ask is not None
                else ZERO
            ),
            "opposite_quote_1sh": _quote_payload(one_quote),
            "opposite_quote_5sh": _quote_payload(five_quote),
            "complete_now_quote": _quote_payload(complete_quote),
            "complete_now_taker_fee": complete_fee,
            "complete_now_net_profit": complete_net,
            "complete_now_net_edge_per_share": complete_edge,
            "midpoint_move_100ms": self._midpoint_move(campaign.market_id, 0.100),
            "midpoint_move_250ms": self._midpoint_move(campaign.market_id, 0.250),
            "midpoint_move_500ms": self._midpoint_move(campaign.market_id, 0.500),
            "surge_active": surge.active,
            "surge_reasons": surge.reasons,
            "updates_per_second": surge.updates_per_second,
            "move_1s": surge.move_1s,
            "move_3s": surge.move_3s,
            "seconds_to_expiry": Decimal(str(self._seconds_to_expiry(campaign.slug))),
            "book_age_a_ms": Decimal(str(max(0.0, (now - book_a.updated_monotonic) * 1000))),
            "book_age_b_ms": Decimal(str(max(0.0, (now - book_b.updated_monotonic) * 1000))),
        }
        self._first_fill_snapshots[campaign.market_id] = snapshot
        self.recorder.write(
            "maker_variant_first_fill_snapshot",
            {
                "strategy": self.strategy_name,
                "market_id": campaign.market_id,
                "slug": campaign.slug,
                **snapshot,
            },
        )

    def _consume_new_trades(
        self,
        engine: ArbitrageEngine,
        pair: MarketPair,
        campaign: VariantCampaign,
    ) -> None:
        had_first = campaign.first_fill_at is not None
        super()._consume_new_trades(engine, pair, campaign)
        if not had_first and campaign.first_fill_at is not None:
            self._capture_first_fill_snapshot(engine, pair, campaign)

    def _finalize(
        self,
        campaign: VariantCampaign,
        pnl: Decimal,
        *,
        status: str,
        action: str,
        extra: dict[str, Any],
    ) -> None:
        enriched = dict(extra)
        snapshot = self._first_fill_snapshots.pop(campaign.market_id, None)
        if snapshot is not None:
            enriched["first_fill_snapshot"] = snapshot
            enriched["first_fill_side"] = snapshot.get("first_fill_side")
            enriched["time_to_first_fill_ms"] = snapshot.get("first_fill_ms")
            enriched["queue_imbalance_at_placement"] = snapshot.get("queue_imbalance_initial")
            enriched["small_queue_at_placement"] = snapshot.get("small_queue_initial")
            enriched["max_queue_at_placement"] = snapshot.get("max_queue_initial")
            enriched["completion_edge_at_first_fill"] = snapshot.get(
                "complete_now_net_edge_per_share"
            )
        enriched["market_episode_id"] = outcome_episode_clusterer.assign(
            campaign.slug,
            window_ms=self.settings.v181_episode_window_ms,
        )
        enriched["episode_window_ms"] = self.settings.v181_episode_window_ms
        super()._finalize(campaign, pnl, status=status, action=action, extra=enriched)


class InstrumentedMultiAssetQueueAwareVariantEngine(
    FirstFillTelemetryMixin, MultiAssetQueueAwareVariantEngine
):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._telemetry_setup()


class InstrumentedPairedMakerVariantEngine(
    FirstFillTelemetryMixin, PairedMakerVariantEngine
):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._telemetry_setup()


class SelectiveHybridVariant(InstrumentedMultiAssetQueueAwareVariantEngine):
    def __init__(
        self,
        settings,
        recorder: JsonlRecorder,
        regime: MarketRegimeTracker,
        *,
        min_imbalance: Decimal,
        small_queue_cap: Decimal | None = None,
    ) -> None:
        super().__init__(
            settings,
            recorder,
            regime,
            mode="HYBRID",
            target_pair=settings.v181_hybrid_pair,
        )
        self.shares = settings.v181_selective_trade_shares
        self.min_imbalance = min_imbalance
        self.small_queue_cap = small_queue_cap
        suffix = f"-Q{format(small_queue_cap.normalize(), 'f')}" if small_queue_cap else ""
        self.strategy_name = (
            f"SHYB-{int(self.target_pair * 100):02d}-"
            f"I{format(min_imbalance.normalize(), 'f')}{suffix}"
        )
        from .strategy_metrics import StrategyEquity

        self.equity = StrategyEquity(self.strategy_name, recorder)
        self.selection_pair_skips = 0
        self.selection_queue_skips = 0
        self.selection_imbalance_skips = 0

    def _maybe_place(
        self,
        engine: ArbitrageEngine,
        pair: MarketPair,
        surge: SurgeSnapshot,
    ) -> None:
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
        best_a = a.best_bid()
        best_b = b.best_bid()
        if best_a is None or best_b is None:
            return

        bid_a, bid_b = target_bids(
            best_a, best_b, self.target_pair, self.settings.maker_tick_size
        )
        combined = bid_a + bid_b
        gross_edge = ONE - combined
        if combined > self.target_pair or gross_edge < self.settings.maker_min_gross_edge_per_share:
            self.selection_pair_skips += 1
            return

        queue_a = a.bids.get(bid_a, ZERO)
        queue_b = b.bids.get(bid_b, ZERO)
        if queue_a <= ZERO or queue_b <= ZERO:
            self.selection_queue_skips += 1
            return
        imbalance = _ratio(queue_a, queue_b)
        if imbalance < self.min_imbalance:
            self.selection_imbalance_skips += 1
            return
        small_queue = min(queue_a, queue_b)
        if self.small_queue_cap is not None and small_queue > self.small_queue_cap:
            self.selection_queue_skips += 1
            return

        if (
            self.settings.maker_use_empirical_risk_gate
            and self.risk.filled_campaigns >= self.settings.maker_empirical_risk_min_samples
            and gross_edge <= self.risk.estimated_reserve_per_share
        ):
            self.risk_skips += 1
            return

        _install_campaign(
            self,
            pair,
            a,
            b,
            bid_a=bid_a,
            bid_b=bid_b,
            queue_a=queue_a,
            queue_b=queue_b,
            gross_edge=gross_edge,
            extra={
                "selective_family": "ASYMMETRIC_HYBRID",
                "min_queue_imbalance": self.min_imbalance,
                "small_queue_cap": self.small_queue_cap,
            },
        )

    def diagnostic_row(self) -> dict[str, Any]:
        row = super().diagnostic_row()
        row.update(
            {
                "selective_v181": True,
                "selective_family": "ASYMMETRIC_HYBRID",
                "min_queue_imbalance": self.min_imbalance,
                "small_queue_cap": self.small_queue_cap,
                "selection_pair_skips": self.selection_pair_skips,
                "selection_queue_skips": self.selection_queue_skips,
                "selection_imbalance_skips": self.selection_imbalance_skips,
            }
        )
        return row


class SelectivePairedMakerVariant(InstrumentedMultiAssetQueueAwareVariantEngine):
    def __init__(
        self,
        settings,
        recorder: JsonlRecorder,
        regime: MarketRegimeTracker,
        *,
        max_pair: Decimal,
        max_queue: Decimal,
    ) -> None:
        super().__init__(
            settings,
            recorder,
            regime,
            mode="MAKER",
            target_pair=max_pair,
        )
        self.shares = settings.v181_selective_trade_shares
        self.max_pair = max_pair
        self.max_queue = max_queue
        self.max_imbalance = settings.v181_selective_pair_max_imbalance
        self.strategy_name = (
            f"SPMAKER-P{int(max_pair * 100):02d}-"
            f"Q{format(max_queue.normalize(), 'f')}"
        )
        from .strategy_metrics import StrategyEquity

        self.equity = StrategyEquity(self.strategy_name, recorder)
        self.selection_pair_skips = 0
        self.selection_queue_skips = 0
        self.selection_imbalance_skips = 0

    def _maybe_place(
        self,
        engine: ArbitrageEngine,
        pair: MarketPair,
        surge: SurgeSnapshot,
    ) -> None:
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
        if combined > self.max_pair:
            self.selection_pair_skips += 1
            return

        queue_a = a.bids.get(bid_a, ZERO)
        queue_b = b.bids.get(bid_b, ZERO)
        if (
            queue_a <= ZERO
            or queue_b <= ZERO
            or queue_a > self.max_queue
            or queue_b > self.max_queue
        ):
            self.selection_queue_skips += 1
            return
        imbalance = _ratio(queue_a, queue_b)
        if imbalance > self.max_imbalance:
            self.selection_imbalance_skips += 1
            return

        if (
            self.settings.maker_use_empirical_risk_gate
            and self.risk.filled_campaigns >= self.settings.maker_empirical_risk_min_samples
            and gross_edge <= self.risk.estimated_reserve_per_share
        ):
            self.risk_skips += 1
            return

        _install_campaign(
            self,
            pair,
            a,
            b,
            bid_a=bid_a,
            bid_b=bid_b,
            queue_a=queue_a,
            queue_b=queue_b,
            gross_edge=gross_edge,
            extra={
                "selective_family": "TINY_QUEUE_PAIRED_MAKER",
                "max_pair": self.max_pair,
                "max_queue_per_leg": self.max_queue,
                "max_queue_imbalance": self.max_imbalance,
            },
        )

    def diagnostic_row(self) -> dict[str, Any]:
        row = super().diagnostic_row()
        row.update(
            {
                "selective_v181": True,
                "selective_family": "TINY_QUEUE_PAIRED_MAKER",
                "max_pair": self.max_pair,
                "max_queue": self.max_queue,
                "max_queue_imbalance": self.max_imbalance,
                "selection_pair_skips": self.selection_pair_skips,
                "selection_queue_skips": self.selection_queue_skips,
                "selection_imbalance_skips": self.selection_imbalance_skips,
            }
        )
        return row


class SelectiveMakerVariant(InstrumentedMultiAssetQueueAwareVariantEngine):
    def __init__(
        self,
        settings,
        recorder: JsonlRecorder,
        regime: MarketRegimeTracker,
        *,
        target_pair: Decimal = Decimal("0.97"),
        max_queue: Decimal = Decimal("10"),
    ) -> None:
        super().__init__(
            settings,
            recorder,
            regime,
            mode="MAKER",
            target_pair=target_pair,
        )
        self.shares = settings.v181_selective_trade_shares
        self.max_queue = max_queue
        self.max_imbalance = settings.v181_selective_pair_max_imbalance
        self.strategy_name = (
            f"SMAKER-{int(target_pair * 100):02d}-"
            f"Q{format(max_queue.normalize(), 'f')}"
        )
        from .strategy_metrics import StrategyEquity

        self.equity = StrategyEquity(self.strategy_name, recorder)
        self.selection_queue_skips = 0
        self.selection_imbalance_skips = 0

    def _maybe_place(
        self,
        engine: ArbitrageEngine,
        pair: MarketPair,
        surge: SurgeSnapshot,
    ) -> None:
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
        best_a = a.best_bid()
        best_b = b.best_bid()
        if best_a is None or best_b is None:
            return
        bid_a, bid_b = target_bids(
            best_a, best_b, self.target_pair, self.settings.maker_tick_size
        )
        gross_edge = ONE - bid_a - bid_b
        if gross_edge < self.settings.maker_min_gross_edge_per_share:
            return

        queue_a = a.bids.get(bid_a, ZERO)
        queue_b = b.bids.get(bid_b, ZERO)
        if (
            queue_a <= ZERO
            or queue_b <= ZERO
            or queue_a > self.max_queue
            or queue_b > self.max_queue
        ):
            self.selection_queue_skips += 1
            return
        imbalance = _ratio(queue_a, queue_b)
        if imbalance > self.max_imbalance:
            self.selection_imbalance_skips += 1
            return

        if (
            self.settings.maker_use_empirical_risk_gate
            and self.risk.filled_campaigns >= self.settings.maker_empirical_risk_min_samples
            and gross_edge <= self.risk.estimated_reserve_per_share
        ):
            self.risk_skips += 1
            return

        _install_campaign(
            self,
            pair,
            a,
            b,
            bid_a=bid_a,
            bid_b=bid_b,
            queue_a=queue_a,
            queue_b=queue_b,
            gross_edge=gross_edge,
            extra={
                "selective_family": "TINY_QUEUE_MAKER",
                "max_queue_per_leg": self.max_queue,
                "max_queue_imbalance": self.max_imbalance,
            },
        )

    def diagnostic_row(self) -> dict[str, Any]:
        row = super().diagnostic_row()
        row.update(
            {
                "selective_v181": True,
                "selective_family": "TINY_QUEUE_MAKER",
                "max_queue": self.max_queue,
                "max_queue_imbalance": self.max_imbalance,
                "selection_queue_skips": self.selection_queue_skips,
                "selection_imbalance_skips": self.selection_imbalance_skips,
            }
        )
        return row
