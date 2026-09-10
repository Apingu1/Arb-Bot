from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from .batch_fok_v187 import PreciseBatchFOKSuiteV187
from .profit_fok_v185_diag import (
    DiagnosedProfitFOKEngineV185,
    NamedDiagnosedProfitFOKEngineV185,
)
from .winner_research_v184 import WinnerResearchSuiteV184


class CorePFOKSuiteV187:
    """Only the sequential PFOK controls requested for Phase 1.8.7.

    PFOK retains the exact inherited settings. PFOK-S10 and PFOK-S20 are the
    same original execution/recovery engine with only fixed size substituted.
    Older 1.8.5/1.8.6 exploratory variants remain untouched on their branches
    but are not run here, reducing duplicate hot-path work.
    """

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


_BATCH_BY_RECORDER: dict[int, PreciseBatchFOKSuiteV187] = {}


class BatchFirstMakerResearchSuiteV187:
    """Run BFOK before the legacy research stack on every update/timer turn."""

    def __init__(self, settings, recorder) -> None:
        self.base = WinnerResearchSuiteV184(settings, recorder)
        self.regime = self.base.regime
        self.batch = PreciseBatchFOKSuiteV187(settings, recorder)
        _BATCH_BY_RECORDER[id(recorder)] = self.batch

    def __getattr__(self, name):
        return getattr(self.base, name)

    def process_due(self, engine) -> None:
        self.batch.process_due(engine)
        self.base.process_due(engine)

    def on_market_update(self, engine, market_id: str) -> None:
        # Refresh surge/regime state first, then evaluate one shared BFOK book
        # snapshot before hedge/frontier/PFOK/atomic research runs.
        self.base.on_market_update(engine, market_id)
        surge = self.regime.current(market_id)
        self.batch.on_market_update(engine, market_id, surge)


class ParallelBatchFOKSuiteV187:
    """Dashboard/report surface: PFOK controls plus Phase 1.8.7 BFOK family."""

    def __init__(self, settings, recorder) -> None:
        self.settings = settings
        self.control = CorePFOKSuiteV187(settings, recorder)
        self.batch = _BATCH_BY_RECORDER.get(id(recorder))
        if self.batch is None:
            self.batch = PreciseBatchFOKSuiteV187(settings, recorder)
            _BATCH_BY_RECORDER[id(recorder)] = self.batch

    def on_market_update(self, engine, market_id: str, surge=None) -> None:
        # BFOK already evaluated earlier by BatchFirstMakerResearchSuiteV187.
        self.control.on_market_update(engine, market_id, surge)

    def process_due(self, engine) -> None:
        # BFOK precise timers/polling already run via the research wrapper.
        self.control.process_due(engine)

    def diagnostic_rows(self):
        return [*self.control.diagnostic_rows(), *self.batch.diagnostic_rows()]

    def ranked_rows(self):
        rows = [row for row in self.diagnostic_rows() if row["placements"] > 0]
        return sorted(
            rows,
            key=lambda row: (row["ev_per_placement"], row["p_both"], -row["p_miss"]),
            reverse=True,
        )
