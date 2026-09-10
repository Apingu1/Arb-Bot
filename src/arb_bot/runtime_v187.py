from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from .batch_fok_raw_v187 import BatchFOKWithRawSuiteV187
from .opportunity_funnel_v187 import OpportunityFunnelV187
from .profit_fok_v185_diag import (
    DiagnosedProfitFOKEngineV185,
    NamedDiagnosedProfitFOKEngineV185,
)
from .winner_research_v184 import WinnerResearchSuiteV184


ZERO = Decimal("0")


class CorePFOKSuiteV187:
    """Only the sequential PFOK controls requested for Phase 1.8.7."""

    def __init__(self, settings, recorder) -> None:
        self.settings = settings
        self.engine = DiagnosedProfitFOKEngineV185(settings, recorder)
        self.variants = [self.engine]
        if settings.v187_keep_pfok_size_controls:
            sample_ms = settings.v185_frontier_gate_sample_interval_ms
            for name, shares in (("PFOK-S10", Decimal("10")), ("PFOK-S20", Decimal("20"))):
                self.variants.append(
                    NamedDiagnosedProfitFOKEngineV185(
                        replace(
                            settings,
                            v185_profit_sizes=(shares,),
                            v185_gate_sample_interval_ms=sample_ms,
                        ),
                        recorder,
                        name,
                    )
                )

    def on_market_update(self, engine, market_id: str, surge=None) -> None:
        for variant in self.variants:
            variant.on_market_update(engine, market_id, surge)

    def process_due(self, engine) -> None:
        for variant in self.variants:
            variant.process_due(engine)

    def diagnostic_rows(self):
        return [variant.diagnostic_row() for variant in self.variants]

    def ranked_rows(self):
        rows = [row for row in self.diagnostic_rows() if row["placements"] > 0]
        return sorted(
            rows,
            key=lambda row: (row["ev_per_placement"], row["p_both"], -row["p_miss"]),
            reverse=True,
        )


_BATCH_BY_RECORDER: dict[int, BatchFOKWithRawSuiteV187] = {}
_FUNNEL_BY_RECORDER: dict[int, OpportunityFunnelV187] = {}


class BatchFirstMakerResearchSuiteV187:
    """Run BFOK-RAW/BFOK first and capture the opportunity funnel."""

    def __init__(self, settings, recorder) -> None:
        self.base = WinnerResearchSuiteV184(settings, recorder)
        self.regime = self.base.regime
        self.batch = BatchFOKWithRawSuiteV187(settings, recorder)
        self.funnel = OpportunityFunnelV187(settings, recorder)
        _BATCH_BY_RECORDER[id(recorder)] = self.batch
        _FUNNEL_BY_RECORDER[id(recorder)] = self.funnel

    def __getattr__(self, name):
        return getattr(self.base, name)

    def process_due(self, engine) -> None:
        self.batch.process_due(engine)
        self.base.process_due(engine)

    def on_market_update(self, engine, market_id: str) -> None:
        # Refresh surge/regime state first. The funnel snapshots the observed
        # market and protected BFOK entry reasons before RAW/BFOK mutate state.
        self.base.on_market_update(engine, market_id)
        surge = self.regime.current(market_id)
        self.funnel.begin_update(engine, market_id, surge, self.batch)
        self.batch.on_market_update(engine, market_id, surge)
        self.funnel.after_batch(market_id, self.batch)


class ParallelBatchFOKSuiteV187:
    """Dashboard/report surface: PFOK controls plus BFOK/BFOK-RAW/funnel."""

    def __init__(self, settings, recorder) -> None:
        self.settings = settings
        self.control = CorePFOKSuiteV187(settings, recorder)
        self.batch = _BATCH_BY_RECORDER.get(id(recorder))
        if self.batch is None:
            self.batch = BatchFOKWithRawSuiteV187(settings, recorder)
            _BATCH_BY_RECORDER[id(recorder)] = self.batch
        self.funnel = _FUNNEL_BY_RECORDER.get(id(recorder))
        if self.funnel is None:
            self.funnel = OpportunityFunnelV187(settings, recorder)
            _FUNNEL_BY_RECORDER[id(recorder)] = self.funnel

    def on_market_update(self, engine, market_id: str, surge=None) -> None:
        # BFOK/BFOK-RAW already ran earlier. Capture PFOK detection state before
        # the PFOK controls mutate pending/candidate counters, then record exact
        # same-update candidate-versus-block attribution for profitable RAW fills.
        self.funnel.before_pfok(engine, market_id, surge, self.control)
        self.control.on_market_update(engine, market_id, surge)
        self.funnel.after_pfok(market_id, self.control)

    def process_due(self, engine) -> None:
        self.control.process_due(engine)

    def _batch_diagnostic_rows(self):
        rows = []
        for variant in self.batch.variants:
            row = dict(variant.diagnostic_row())
            miss_values = list(getattr(variant, "miss_losses_per_share", []))
            avg_miss = sum(miss_values, ZERO) / Decimal(len(miss_values)) if miss_values else ZERO
            is_raw = variant.strategy == "BFOK-RAW"
            row.update(
                {
                    "opportunities": int(row.get("candidates") or 0),
                    "lifetime_samples": int(row.get("completed") or 0),
                    "shares": (
                        format(variant.fixed_size.normalize(), "f")
                        if variant.fixed_size is not None
                        else "EV"
                    ),
                    "edge_target": Decimal("-10") if is_raw else self.settings.v187_detection_min_edge_per_share,
                    "arrival_skew_ms": 0,
                    "coverage_multiple": ZERO if is_raw else self.settings.v187_detection_coverage_multiple,
                    "stability_ms": 0,
                    "base_latency_ms": 0 if is_raw else self.settings.v187_batch_arrival_latency_ms,
                    "avg_detected_edge": ZERO,
                    "avg_detected_coverage": ZERO,
                    "avg_miss_loss_per_share": avg_miss,
                    "recovery_completions": variant.recovery_completions,
                    "recovery_unwinds": variant.recovery_unwinds,
                    "recovery_liquidity_failures": variant.recovery_liquidity_failures,
                    "surge_blocks": variant.surge_blocks,
                }
            )
            rows.append(row)
        return rows

    def diagnostic_rows(self):
        return [*self.control.diagnostic_rows(), *self._batch_diagnostic_rows()]

    def ranked_rows(self):
        rows = [row for row in self.diagnostic_rows() if row["placements"] > 0]
        return sorted(
            rows,
            key=lambda row: (row["ev_per_placement"], row["p_both"], -row["p_miss"]),
            reverse=True,
        )
