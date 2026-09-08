from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from .fees import taker_fee
from .maker_research import ONE, ZERO, VariantCampaign, _quote_payload, _utc_now
from .models import MarketPair
from .selective_research_v181 import (
    SelectiveHybridVariant as SelectiveHybridVariantV181,
    SelectiveMakerVariant as SelectiveMakerVariantV181,
    SelectivePairedMakerVariant as SelectivePairedMakerVariantV181,
)
from .strategy import ArbitrageEngine


POST_FILL_CHECKPOINTS_MS = (25, 50, 100, 250, 500)


class FillEdgeTimelineMixin:
    """Phase 1.8.2 observational telemetry for selective maker fills.

    This mixin does not alter placement, fill, completion, unwind, or risk logic.
    It only records the state of the hedgeable complete-set edge at the first
    fill and at coarse post-fill checkpoints while residual inventory remains.
    """

    def _telemetry_setup(self) -> None:
        super()._telemetry_setup()
        self._post_fill_edge_timeline: dict[str, dict[str, dict[str, Any]]] = {}

    def _current_completion_sample(
        self,
        engine: ArbitrageEngine,
        pair: MarketPair,
        campaign: VariantCampaign,
    ) -> dict[str, Any] | None:
        exposure_leg, exposure_qty, exposure_avg = self._exposure_cost(campaign)
        if exposure_leg is None or exposure_qty <= ZERO:
            return None

        opposite_book = engine.books.get(
            pair.token_b if exposure_leg == "A" else pair.token_a
        )
        if opposite_book is None or not opposite_book.ready:
            return None

        quote = opposite_book.quote_buy(exposure_qty)
        if quote is None:
            return {
                "exposure_leg": exposure_leg,
                "exposure_qty": exposure_qty,
                "exposure_average_cost": exposure_avg,
                "opposite_best_ask": opposite_book.best_ask(),
                "opposite_depth_at_best_ask": ZERO,
                "complete_now_quote": None,
                "complete_now_taker_fee": ZERO,
                "complete_now_net_profit": None,
                "complete_now_net_edge_per_share": None,
            }

        fee = taker_fee(quote.segments, self.settings.crypto_taker_fee_rate)
        net = exposure_qty - exposure_qty * exposure_avg - quote.notional - fee
        edge = net / exposure_qty
        best_ask = opposite_book.best_ask()
        return {
            "exposure_leg": exposure_leg,
            "exposure_qty": exposure_qty,
            "exposure_average_cost": exposure_avg,
            "opposite_best_ask": best_ask,
            "opposite_depth_at_best_ask": (
                opposite_book.asks.get(best_ask, ZERO) if best_ask is not None else ZERO
            ),
            "complete_now_quote": _quote_payload(quote),
            "complete_now_taker_fee": fee,
            "complete_now_net_profit": net,
            "complete_now_net_edge_per_share": edge,
        }

    def _augment_first_fill_snapshot(self, campaign: VariantCampaign) -> None:
        snapshot = self._first_fill_snapshots.get(campaign.market_id)
        if not isinstance(snapshot, dict) or snapshot.get("phase182_enriched"):
            return

        quoted_gross_edge = ONE - campaign.leg_a.price - campaign.leg_b.price
        fill_edge = snapshot.get("complete_now_net_edge_per_share")
        deterioration = (
            Decimal(str(fill_edge)) - quoted_gross_edge
            if fill_edge is not None
            else None
        )
        snapshot.update(
            {
                "phase182_enriched": True,
                "maker_gross_edge_at_placement": quoted_gross_edge,
                "edge_deterioration_vs_quoted_gross": deterioration,
            }
        )
        self.recorder.write(
            "maker_variant_first_fill_timing_v182",
            {
                "strategy": self.strategy_name,
                "market_id": campaign.market_id,
                "slug": campaign.slug,
                "captured_at": snapshot.get("captured_at") or _utc_now(),
                "first_fill_side": snapshot.get("first_fill_side"),
                "first_fill_ms": snapshot.get("first_fill_ms"),
                "maker_gross_edge_at_placement": quoted_gross_edge,
                "complete_now_net_edge_per_share": fill_edge,
                "edge_deterioration_vs_quoted_gross": deterioration,
                "queue_imbalance_initial": snapshot.get("queue_imbalance_initial"),
                "small_queue_initial": snapshot.get("small_queue_initial"),
                "max_queue_initial": snapshot.get("max_queue_initial"),
            },
        )

    def _sample_post_fill_edge(
        self,
        engine: ArbitrageEngine,
        pair: MarketPair,
        campaign: VariantCampaign,
    ) -> None:
        if campaign.first_fill_at is None or campaign.full:
            return

        elapsed_ms = Decimal(str((time.monotonic() - campaign.first_fill_at) * 1000))
        timeline = self._post_fill_edge_timeline.setdefault(campaign.market_id, {})

        for checkpoint in POST_FILL_CHECKPOINTS_MS:
            key = str(checkpoint)
            if key in timeline or elapsed_ms < Decimal(checkpoint):
                continue
            sample = self._current_completion_sample(engine, pair, campaign)
            if sample is None:
                continue
            sample = {
                "target_elapsed_ms": Decimal(checkpoint),
                "actual_elapsed_ms": elapsed_ms,
                "sampled_at": _utc_now(),
                **sample,
            }
            timeline[key] = sample
            self.recorder.write(
                "maker_variant_post_fill_edge_sample",
                {
                    "strategy": self.strategy_name,
                    "market_id": campaign.market_id,
                    "slug": campaign.slug,
                    **sample,
                },
            )

    def _consume_new_trades(
        self,
        engine: ArbitrageEngine,
        pair: MarketPair,
        campaign: VariantCampaign,
    ) -> None:
        super()._consume_new_trades(engine, pair, campaign)
        if campaign.first_fill_at is not None:
            self._augment_first_fill_snapshot(campaign)
            self._sample_post_fill_edge(engine, pair, campaign)

    def _finalize(
        self,
        campaign: VariantCampaign,
        pnl: Decimal,
        *,
        status: str,
        action: str,
        extra: dict[str, Any],
    ) -> None:
        now = time.monotonic()
        enriched = dict(extra)
        timeline = self._post_fill_edge_timeline.pop(campaign.market_id, None)
        if timeline:
            enriched["post_fill_edge_timeline"] = timeline
        if campaign.first_fill_at is not None:
            enriched["first_fill_to_finalize_ms"] = Decimal(
                str((now - campaign.first_fill_at) * 1000)
            )
        enriched["campaign_age_ms"] = Decimal(str((now - campaign.placed_at) * 1000))
        super()._finalize(campaign, pnl, status=status, action=action, extra=enriched)


class SelectiveHybridVariantV182(FillEdgeTimelineMixin, SelectiveHybridVariantV181):
    pass


class SelectivePairedMakerVariantV182(
    FillEdgeTimelineMixin, SelectivePairedMakerVariantV181
):
    pass


class SelectiveMakerVariantV182(FillEdgeTimelineMixin, SelectiveMakerVariantV181):
    pass
