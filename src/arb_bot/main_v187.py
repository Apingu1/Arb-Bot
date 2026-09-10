from __future__ import annotations

from . import main as _base
from . import runtime_controls_v18 as _runtime_controls_v18
from .atomic_benchmark_v184 import AtomicExecutionBenchmarkSuiteV184, log_atomic_diagnostics_v184
from .config_v187 import SettingsV187
from .dashboard_v187 import DashboardServerV187, DashboardStateV187
from .main_v182 import InactiveTakerExecutor
from .research_context_v187 import PHASE187_RUN_ID
from .runtime_v187 import BatchFirstMakerResearchSuiteV187, ParallelBatchFOKSuiteV187
from .storage_v186 import LowLatencyJsonlRecorderV186
from .strategy_v186 import ArbitrageEngineV186


def cli() -> None:
    # Historical maker/taker runtime controls stay OFF. Phase 1.8.7 focuses on
    # PFOK/PFOK-S10/PFOK-S20 controls versus parallel BFOK alternatives.
    for model in list(_runtime_controls_v18.DEFAULT_PROFILE):
        _runtime_controls_v18.DEFAULT_PROFILE[model] = set()

    _base.Settings = SettingsV187
    _base.ArbitrageEngine = ArbitrageEngineV186
    _base.JsonlRecorder = LowLatencyJsonlRecorderV186
    _base.MakerResearchSuite = BatchFirstMakerResearchSuiteV187
    _base.ShadowExecutor = InactiveTakerExecutor
    _base.DualFOKResearchSuite = ParallelBatchFOKSuiteV187
    _base.IdealAtomicBenchmarkSuite = AtomicExecutionBenchmarkSuiteV184
    _base.log_atomic_diagnostics = log_atomic_diagnostics_v184
    _base.DashboardState = DashboardStateV187
    _base.DashboardServer = DashboardServerV187

    _ = PHASE187_RUN_ID
    _base.cli()


if __name__ == "__main__":
    cli()
