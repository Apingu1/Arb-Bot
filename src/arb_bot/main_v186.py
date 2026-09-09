from __future__ import annotations

from . import main as _base
from .atomic_benchmark_v184 import AtomicExecutionBenchmarkSuiteV184, log_atomic_diagnostics_v184
from .config_v186 import SettingsV186
from .dashboard_v186 import DashboardServerV186, DashboardStateV186
from .latency_runtime_v186 import LatencyFirstDualFOKSuiteV186, LatencyFirstMakerResearchSuiteV186
from .main_v182 import InactiveTakerExecutor
from .research_context_v186 import PHASE186_RUN_ID
from .storage_v186 import LowLatencyJsonlRecorderV186
from .strategy_v186 import ArbitrageEngineV186
from . import runtime_controls_v18 as _runtime_controls_v18


def cli() -> None:
    # Keep all historical maker/taker controls disabled exactly as in 1.8.5.
    for model in list(_runtime_controls_v18.DEFAULT_PROFILE):
        _runtime_controls_v18.DEFAULT_PROFILE[model] = set()

    _base.Settings = SettingsV186
    _base.ArbitrageEngine = ArbitrageEngineV186
    _base.JsonlRecorder = LowLatencyJsonlRecorderV186
    _base.MakerResearchSuite = LatencyFirstMakerResearchSuiteV186
    _base.ShadowExecutor = InactiveTakerExecutor
    _base.DualFOKResearchSuite = LatencyFirstDualFOKSuiteV186
    _base.IdealAtomicBenchmarkSuite = AtomicExecutionBenchmarkSuiteV184
    _base.log_atomic_diagnostics = log_atomic_diagnostics_v184
    _base.DashboardState = DashboardStateV186
    _base.DashboardServer = DashboardServerV186

    _ = PHASE186_RUN_ID
    _base.cli()


if __name__ == "__main__":
    cli()
