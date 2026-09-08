from __future__ import annotations

import time
from decimal import Decimal

from .maker_research import ZERO, _utc_now, target_bids
from .selective_research_v184 import (
    SelectiveHybridVariantV184,
    SelectiveMakerVariantV184,
    SelectivePairedMakerVariantV184,
)
from .winner_research_v183 import WinnerResearchSuiteV183


class FastEntryAndTimerGuardMixinV184:
    """Fast safe-entry and deterministic quote-age protection.

    The placement gate avoids sending a selective shadow maker quote when its
    maker-first/taker-second economics are already at or beyond the toxic
    cancellation boundary. The timer guard then enforces stale/hard quote age
    even if the books do not update for a short period.
    """

    def _v184_candidate_maker_prices(self, best_a: Decimal, best_b: Decimal):
        # SelectivePairedMaker joins the current best bids directly. SHYB/SMAKER
        # use the target-pair transformation inherited from the maker engine.
        if hasattr(self, "max_pair"):
            return best_a, best_b
        return target_bids(
            best_a,
            best_b,
            self.target_pair,
            self.settings.maker_tick_size,
        )

    def _maybe_place(self, engine, pair, surge) -> None:
        if self.settings.v184_placement_edge_gate_enabled:
            book_a = engine.books.get(pair.token_a)
            book_b = engine.books.get(pair.token_b)
            if (
                book_a is not None
                and book_b is not None
                and book_a.ready
                and book_b.ready
                and book_a.best_bid() is not None
                and book_b.best_bid() is not None
            ):
                now = time.monotonic()
                age_a = Decimal(
                    str(max(0.0, (now - book_a.updated_monotonic) * 1000))
                )
                age_b = Decimal(
                    str(max(0.0, (now - book_b.updated_monotonic) * 1000))
                )
                bid_a, bid_b = self._v184_candidate_maker_prices(
                    book_a.best_bid(), book_b.best_bid()
                )
                a_first = self._maker_plus_taker_edge(
                    bid_a, book_b, self.shares
                )
                b_first = self._maker_plus_taker_edge(
                    bid_b, book_a, self.shares
                )
                raw_a = a_first.get("edge_per_share")
                raw_b = b_first.get("edge_per_share")
                edge_a = Decimal(str(raw_a)) if raw_a is not None else None
                edge_b = Decimal(str(raw_b)) if raw_b is not None else None

                reject_reason = None
                worst_edge = None
                if edge_a is None or edge_b is None:
                    reject_reason = "NO_FULL_SIZE_HEDGE_QUOTE"
                else:
                    worst_edge = min(edge_a, edge_b)
                    if worst_edge <= self.settings.v184_placement_min_edge_per_share:
                        reject_reason = "TOXIC_EDGE_AT_PLACEMENT"
                    elif max(age_a, age_b) >= Decimal(
                        self.settings.v184_max_opposite_book_age_ms
                    ):
                        reject_reason = "STALE_BOOK_AT_PLACEMENT"

                if reject_reason is not None:
                    self.v184_placement_edge_skips = (
                        getattr(self, "v184_placement_edge_skips", 0) + 1
                    )
                    self.recorder.write(
                        "maker_variant_placement_reject_v184",
                        {
                            "phase184_run_id": getattr(
                                __import__(
                                    "arb_bot.research_context_v184",
                                    fromlist=["PHASE184_RUN_ID"],
                                ),
                                "PHASE184_RUN_ID",
                            ),
                            "strategy": self.strategy_name,
                            "market_id": pair.market_id,
                            "slug": pair.slug,
                            "rejected_at": _utc_now(),
                            "reason": reject_reason,
                            "candidate_maker_bid_a": bid_a,
                            "candidate_maker_bid_b": bid_b,
                            "if_a_first_edge_per_share": edge_a,
                            "if_b_first_edge_per_share": edge_b,
                            "worst_edge_per_share": worst_edge,
                            "minimum_placement_edge_per_share": self.settings.v184_placement_min_edge_per_share,
                            "book_age_a_ms": age_a,
                            "book_age_b_ms": age_b,
                        },
                    )
                    return

        super()._maybe_place(engine, pair, surge)

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

    def diagnostic_row(self):
        row = super().diagnostic_row()
        row["v184_placement_edge_skips"] = getattr(
            self, "v184_placement_edge_skips", 0
        )
        return row


class GuardedSelectiveHybridVariantV184(
    FastEntryAndTimerGuardMixinV184, SelectiveHybridVariantV184
):
    pass


class GuardedSelectivePairedMakerVariantV184(
    FastEntryAndTimerGuardMixinV184, SelectivePairedMakerVariantV184
):
    pass


class GuardedSelectiveMakerVariantV184(
    FastEntryAndTimerGuardMixinV184, SelectiveMakerVariantV184
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
