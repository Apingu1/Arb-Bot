from __future__ import annotations

from . import main as _base
from . import runtime_controls_v18 as _runtime_controls_v18
from .config_v189 import SettingsV189
from .dashboard_v187 import DashboardServerV187, DashboardStateV187
from .main_v182 import InactiveTakerExecutor
from .research_context_v189 import PHASE189_RUN_ID
from .runtime_v189 import (
    InactiveAuxiliarySuiteV189,
    LatencyIsolationResearchSuiteV189,
    ParallelBatchFOKSuiteV189,
)
from .storage_v186 import LowLatencyJsonlRecorderV186
from .strategy_v186 import ArbitrageEngineV186


class InactiveAtomicBenchmarkV189:
    """Remove unrelated atomic benchmark work from the latency hot path."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def on_market_update(self, *args, **kwargs) -> None:
        return None

    def process_due(self, *args, **kwargs) -> None:
        return None

    def diagnostic_rows(self):
        return []

    def asset_rows(self):
        return []


def _no_atomic_diagnostics(*args, **kwargs) -> None:
    return None


def cli() -> None:
    # Phase 1.8.9 is intentionally narrow: protected PFOK/BFOK controls,
    # controlled latency frontier, and the lightweight RAW observer only.
    # Disable legacy maker/hybrid runtime profiles and unrelated hedge/EV/atomic
    # suites so local scheduler measurements are not polluted by research load.
    for model in list(_runtime_controls_v18.DEFAULT_PROFILE):
        _runtime_controls_v18.DEFAULT_PROFILE[model] = set()

    _base.Settings = SettingsV189
    _base.ArbitrageEngine = ArbitrageEngineV186
    _base.JsonlRecorder = LowLatencyJsonlRecorderV186
    _base.MakerResearchSuite = LatencyIsolationResearchSuiteV189
    _base.HedgeableResearchSuite = InactiveAuxiliarySuiteV189
    _base.CorrectedEVFrontierSuite = InactiveAuxiliarySuiteV189
    _base.ShadowExecutor = InactiveTakerExecutor
    _base.DualFOKResearchSuite = ParallelBatchFOKSuiteV189
    _base.IdealAtomicBenchmarkSuite = InactiveAtomicBenchmarkV189
    _base.log_atomic_diagnostics = _no_atomic_diagnostics
    _base.DashboardState = DashboardStateV187
    _base.DashboardServer = DashboardServerV187

    _ = PHASE189_RUN_ID
    _base.cli()


if __name__ == "__main__":
    cli()
