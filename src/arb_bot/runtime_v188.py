from __future__ import annotations

from decimal import Decimal

from .batch_fok_raw_v187 import BatchFOKWithRawSuiteV187
from .freshness_frontier_v188 import FreshnessFrontierSuiteV188
from .opportunity_funnel_v187 import OpportunityFunnelV187
from .runtime_v187 import CorePFOKSuiteV187
from .winner_research_v184 import WinnerResearchSuiteV184


ZERO = Decimal("0")

_BATCH_BY_RECORDER: dict[int, BatchFOKWithRawSuiteV187] = {}
_FUNNEL_BY_RECORDER: dict[int, OpportunityFunnelV187] = {}
_FRESHNESS_BY_RECORDER: dict[int, FreshnessFrontierSuiteV188] = {}


class BatchFirstMakerResearchSuiteV188:
    """Run RAW/protected BFOK, freshness frontier and compact funnel first."""

    def __init__(self, settings, recorder) -> None:
        self.settings = settings
        self.recorder = recorder
        self.base = WinnerResearchSuiteV184(settings, recorder)
        self.regime = self.base.regime
        self.batch = BatchFOKWithRawSuiteV187(settings, recorder)
        self.funnel = OpportunityFunnelV187(settings, recorder)
        self.freshness = FreshnessFrontierSuiteV188(settings, recorder)
        _BATCH_BY_RECORDER[id(recorder)] = self.batch
        _FUNNEL_BY_RECORDER[id(recorder)] = self.funnel
        _FRESHNESS_BY_RECORDER[id(recorder)] = self.freshness

    def __getattr__(self, name):
        return getattr(self.base, name)

    def _record_raw_win_age(self, engine, market_id: str, before_wins: int, before_equity: Decimal) -> None:
        raw = self.batch.raw
        snapshot = self.batch.last_raw_snapshot
        if raw is None or snapshot is None or raw.wins <= before_wins:
            return
        raw_size = self.settings.v187_raw_size
        metrics = snapshot.metrics.get(raw_size)
        age_a, age_b, skew = self.freshness._ages_at_snapshot(engine, snapshot)
        self.recorder.write(
            "raw_win_age_v188",
            {
                "market_id": market_id,
                "slug": snapshot.pair.slug,
                "asset": snapshot.pair.slug.split("-")[0].upper() if snapshot.pair.slug else "UNKNOWN",
                "shares": raw_size,
                "raw_pnl": raw.equity.equity - before_equity,
                "raw_edge_per_share": metrics.get("edge") if metrics is not None else None,
                "book_age_a_ms": age_a,
                "book_age_b_ms": age_b,
                "older_book_age_ms": max(age_a, age_b) if age_a is not None and age_b is not None else None,
                "book_age_skew_ms": skew,
                "source_strategy": "BFOK-RAW",
                "zero_latency_upper_bound": True,
            },
        )

    def process_due(self, engine) -> None:
        self.batch.process_due(engine)
        self.freshness.process_due(engine)
        self.base.process_due(engine)

    def on_market_update(self, engine, market_id: str) -> None:
        # Update regime first, then snapshot entry state before any strategy
        # mutates counters. RAW/protected BFOK share the existing v1.8.7 path.
        self.base.on_market_update(engine, market_id)
        surge = self.regime.current(market_id)
        self.funnel.begin_update(engine, market_id, surge, self.batch)

        raw = self.batch.raw
        before_wins = raw.equity.wins if raw is not None else 0
        before_equity = raw.equity.equity if raw is not None else ZERO
        self.batch.on_market_update(engine, market_id, surge)
        self._record_raw_win_age(engine, market_id, before_wins, before_equity)

        # The freshness frontier reuses RAW's ungated one-share snapshot so the
        # experiment does not rebuild five separate order-book quotes. Only the
        # permitted age ceiling differs between frontier variants.
        self.freshness.on_market_update(
            engine,
            market_id,
            surge,
            self.batch.last_raw_snapshot,
        )
        self.funnel.after_batch(market_id, self.batch)


class ParallelBatchFOKSuiteV188:
    """Dashboard/report surface for unchanged controls plus freshness research."""

    def __init__(self, settings, recorder) -> None:
        self.settings = settings
        self.control = CorePFOKSuiteV187(settings, recorder)
        self.batch = _BATCH_BY_RECORDER.get(id(recorder))
        self.funnel = _FUNNEL_BY_RECORDER.get(id(recorder))
        self.freshness = _FRESHNESS_BY_RECORDER.get(id(recorder))
        if self.batch is None:
            self.batch = BatchFOKWithRawSuiteV187(settings, recorder)
            _BATCH_BY_RECORDER[id(recorder)] = self.batch
        if self.funnel is None:
            self.funnel = OpportunityFunnelV187(settings, recorder)
            _FUNNEL_BY_RECORDER[id(recorder)] = self.funnel
        if self.freshness is None:
            self.freshness = FreshnessFrontierSuiteV188(settings, recorder)
            _FRESHNESS_BY_RECORDER[id(recorder)] = self.freshness

    def on_market_update(self, engine, market_id: str, surge=None) -> None:
        self.funnel.before_pfok(engine, market_id, surge, self.control)
        self.control.on_market_update(engine, market_id, surge)
        self.funnel.after_pfok(market_id, self.control)

    def process_due(self, engine) -> None:
        self.control.process_due(engine)

    def _row_for_variant(self, variant, *, raw: bool = False) -> dict:
        row = dict(variant.diagnostic_row())
        miss_values = list(getattr(variant, "miss_losses_per_share", []))
        avg_miss = sum(miss_values, ZERO) / Decimal(len(miss_values)) if miss_values else ZERO
        max_age = getattr(variant, "max_book_age_ms", None)
        if max_age is None and not raw:
            max_age = getattr(variant.settings, "v187_max_book_age_ms", None)
        row.update(
            {
                "opportunities": int(row.get("candidates") or 0),
                "lifetime_samples": int(row.get("completed") or 0),
                "shares": (
                    format(variant.fixed_size.normalize(), "f")
                    if variant.fixed_size is not None
                    else "EV"
                ),
                "edge_target": Decimal("-10") if raw else self.settings.v187_detection_min_edge_per_share,
                "arrival_skew_ms": 0,
                "coverage_multiple": ZERO if raw else self.settings.v187_detection_coverage_multiple,
                "stability_ms": 0,
                "base_latency_ms": 0 if raw else self.settings.v187_batch_arrival_latency_ms,
                "avg_detected_edge": ZERO,
                "avg_detected_coverage": ZERO,
                "avg_miss_loss_per_share": avg_miss,
                "recovery_completions": variant.recovery_completions,
                "recovery_unwinds": variant.recovery_unwinds,
                "recovery_liquidity_failures": variant.recovery_liquidity_failures,
                "surge_blocks": variant.surge_blocks,
                "max_book_age_ms": max_age,
            }
        )
        return row

    def diagnostic_rows(self):
        batch_rows = [
            self._row_for_variant(v, raw=(v.strategy == "BFOK-RAW"))
            for v in self.batch.variants
        ]
        freshness_rows = [self._row_for_variant(v) for v in self.freshness.variants]
        return [*self.control.diagnostic_rows(), *batch_rows, *freshness_rows]

    def ranked_rows(self):
        rows = [row for row in self.diagnostic_rows() if row["placements"] > 0]
        return sorted(
            rows,
            key=lambda row: (row["ev_per_placement"], row["p_both"], -row["p_miss"]),
            reverse=True,
        )
