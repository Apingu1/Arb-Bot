from __future__ import annotations

import time
from decimal import Decimal

from .maker_research import ZERO
from .selective_research_v184 import (
    SelectiveHybridVariantV184,
    SelectiveMakerVariantV184,
    SelectivePairedMakerVariantV184,
)
from .winner_research_v183 import WinnerResearchSuiteV183


class TimerAgeGuardMixinV184:
    """Evaluate stale/hard quote-age guards even when the books are unchanged.

    The normal Phase 1.8.4 edge sampler deliberately caches identical book
    states to keep the hot path cheap. Quote age, however, advances with wall
    time. This timer-only guard makes the 500 ms stale rule and 1,000 ms hard
    quote cap deterministic even during a short market-data lull.
    """

    def _v184_latest_prefill_edges(self, market_id: str):
        history = self._v184_prefill_history.get(market_id, {})
        edges = []
        for side in ("A", "B"):
            rows = history.get(side)
            if not rows:
                continue
            edge = rows[-1].get("edge_per_share")
            if edge is not None:
                edges.append((side, Decimal(str(edge))))
        return edges

    def process_due(self, engine) -> None:
        if self.settings.v184_fast_cancel_enabled:
            now = time.monotonic()
            for market_id in list(self.campaigns):
                campaign = self.campaigns.get(market_id)
                if (
                    campaign is None
                    or campaign.any_fill
                    or market_id in self._v184_pending_cancel
                ):
                    continue

                quote_age_ms = Decimal(str(max(0.0, (now - campaign.placed_at) * 1000)))
                latest_edges = self._v184_latest_prefill_edges(market_id)
                trigger_side = None
                worst_edge = None
                if latest_edges:
                    trigger_side, worst_edge = min(latest_edges, key=lambda item: item[1])

                reason = None
                if quote_age_ms >= Decimal(self.settings.v184_hard_quote_age_ms):
                    reason = "HARD_QUOTE_AGE"
                elif (
                    quote_age_ms >= Decimal(self.settings.v184_stale_quote_age_ms)
                    and worst_edge is not None
                    and worst_edge <= self.settings.v184_stale_max_edge_per_share
                ):
                    reason = "STALE_NONPOSITIVE_EDGE"

                if reason is None:
                    continue

                pair = engine.pairs.get(market_id)
                book_a = engine.books.get(pair.token_a) if pair is not None else None
                book_b = engine.books.get(pair.token_b) if pair is not None else None
                age_a = (
                    Decimal(str(max(0.0, (now - book_a.updated_monotonic) * 1000)))
                    if book_a is not None and book_a.updated_monotonic > 0
                    else ZERO
                )
                age_b = (
                    Decimal(str(max(0.0, (now - book_b.updated_monotonic) * 1000)))
                    if book_b is not None and book_b.updated_monotonic > 0
                    else ZERO
                )
                self._v184_arm_cancel(
                    campaign,
                    now=now,
                    reason=reason,
                    trigger_side=trigger_side,
                    trigger_edge=worst_edge,
                    quote_age_ms=quote_age_ms,
                    book_age_a_ms=age_a,
                    book_age_b_ms=age_b,
                )

        super().process_due(engine)


class GuardedSelectiveHybridVariantV184(
    TimerAgeGuardMixinV184, SelectiveHybridVariantV184
):
    pass


class GuardedSelectivePairedMakerVariantV184(
    TimerAgeGuardMixinV184, SelectivePairedMakerVariantV184
):
    pass


class GuardedSelectiveMakerVariantV184(
    TimerAgeGuardMixinV184, SelectiveMakerVariantV184
):
    pass


class WinnerResearchSuiteV184(WinnerResearchSuiteV183):
    """Phase 1.8.3 suite with Phase 1.8.4 fast adverse-selection execution."""

    def __init__(self, settings, recorder) -> None:
        super().__init__(settings, recorder)

        self.selective_hybrids = []
        self.selective_paired_makers = []
        self.selective_makers = []
        if settings.v181_selective_enabled:
            self.selective_hybrids = [
                GuardedSelectiveHybridVariantV184(
                    settings,
                    recorder,
                    self.regime,
                    min_imbalance=Decimal("2"),
                ),
                GuardedSelectiveHybridVariantV184(
                    settings,
                    recorder,
                    self.regime,
                    min_imbalance=Decimal("3"),
                ),
                GuardedSelectiveHybridVariantV184(
                    settings,
                    recorder,
                    self.regime,
                    min_imbalance=Decimal("4"),
                ),
                GuardedSelectiveHybridVariantV184(
                    settings,
                    recorder,
                    self.regime,
                    min_imbalance=Decimal("2"),
                    small_queue_cap=settings.v181_hybrid_small_queue_cap,
                ),
            ]
            self.selective_paired_makers = [
                GuardedSelectivePairedMakerVariantV184(
                    settings,
                    recorder,
                    self.regime,
                    max_pair=Decimal("0.95"),
                    max_queue=Decimal("10"),
                ),
                GuardedSelectivePairedMakerVariantV184(
                    settings,
                    recorder,
                    self.regime,
                    max_pair=Decimal("0.97"),
                    max_queue=Decimal("10"),
                ),
                GuardedSelectivePairedMakerVariantV184(
                    settings,
                    recorder,
                    self.regime,
                    max_pair=Decimal("0.97"),
                    max_queue=Decimal("15"),
                ),
                GuardedSelectivePairedMakerVariantV184(
                    settings,
                    recorder,
                    self.regime,
                    max_pair=Decimal("0.97"),
                    max_queue=Decimal("25"),
                ),
            ]
            self.selective_makers = [
                GuardedSelectiveMakerVariantV184(
                    settings,
                    recorder,
                    self.regime,
                    target_pair=Decimal("0.97"),
                    max_queue=Decimal("10"),
                )
            ]

        self.selective_variants = [
            *self.selective_hybrids,
            *self.selective_paired_makers,
            *self.selective_makers,
        ]
        self.variants = [
            *self.makers,
            *self.hybrids,
            *self.paired_makers,
            *self.selective_variants,
        ]
