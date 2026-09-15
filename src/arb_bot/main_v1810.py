from __future__ import annotations

from . import main as _base
from . import runtime_controls_v18 as _runtime_controls_v18
from .config_v1810 import SettingsV1810
from .dashboard_v187 import DashboardServerV187, DashboardStateV187
from .diagnostics_v189 import log_latency_isolation_diagnostics_v189
from .main_v182 import InactiveTakerExecutor
from .main_v189 import InactiveAtomicBenchmarkV189, _no_atomic_diagnostics
from .research_context_v1810 import PHASE1810_RUN_ID
from .runtime_v189 import (
    InactiveAuxiliarySuiteV189,
    LatencyIsolationResearchSuiteV189,
    ParallelBatchFOKSuiteV189,
)
from .storage_v1810 import LowLatencyJsonlRecorderV1810
from .strategy_v186 import ArbitrageEngineV186


def cli() -> None:
    # Execution behaviour is intentionally identical to Phase 1.8.9.  This
    # phase changes attribution, archival and reporting only.
    for model in list(_runtime_controls_v18.DEFAULT_PROFILE):
        _runtime_controls_v18.DEFAULT_PROFILE[model] = set()

    _base.Settings = SettingsV1810
    _base.ArbitrageEngine = ArbitrageEngineV186
    _base.JsonlRecorder = LowLatencyJsonlRecorderV1810
    _base.MakerResearchSuite = LatencyIsolationResearchSuiteV189
    _base.HedgeableResearchSuite = InactiveAuxiliarySuiteV189
    _base.CorrectedEVFrontierSuite = InactiveAuxiliarySuiteV189
    _base.ShadowExecutor = InactiveTakerExecutor
    _base.DualFOKResearchSuite = ParallelBatchFOKSuiteV189
    _base.IdealAtomicBenchmarkSuite = InactiveAtomicBenchmarkV189
    _base.log_dual_fok_diagnostics = log_latency_isolation_diagnostics_v189
    _base.log_atomic_diagnostics = _no_atomic_diagnostics
    _base.DashboardState = DashboardStateV187
    _base.DashboardServer = DashboardServerV187

    _ = PHASE1810_RUN_ID
    _base.cli()


if __name__ == "__main__":
    cli()
