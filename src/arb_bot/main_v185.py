from __future__ import annotations

from . import main as _base
from .atomic_benchmark_v184 import AtomicExecutionBenchmarkSuiteV184, log_atomic_diagnostics_v184
from .config_v185 import SettingsV185
from .dashboard_v18 import DashboardServerV18, DashboardStateV18
from .main_v182 import InactiveTakerExecutor
from .profit_fok_v185_diag import DiagnosedProfitFOKSuiteV185
from .research_context_v185 import PHASE185_RUN_ID
from .winner_research_v184 import WinnerResearchSuiteV184
from . import runtime_controls_v18 as _runtime_controls_v18


def cli() -> None:
    # Historical maker engines remain available in the UI, but Phase 1.8.5
    # starts them OFF. The profit-focused branch should not silently re-enable
    # known-negative baseline models in a fresh Codespace.
    for model in list(_runtime_controls_v18.DEFAULT_PROFILE):
        _runtime_controls_v18.DEFAULT_PROFILE[model] = set()

    _base.Settings = SettingsV185
    _base.MakerResearchSuite = WinnerResearchSuiteV184
    _base.ShadowExecutor = InactiveTakerExecutor
    _base.DualFOKResearchSuite = DiagnosedProfitFOKSuiteV185
    _base.IdealAtomicBenchmarkSuite = AtomicExecutionBenchmarkSuiteV184
    _base.log_atomic_diagnostics = log_atomic_diagnostics_v184
    _base.DashboardState = DashboardStateV18
    _base.DashboardServer = DashboardServerV18

    _ = PHASE185_RUN_ID
    _base.cli()


if __name__ == "__main__":
    cli()
