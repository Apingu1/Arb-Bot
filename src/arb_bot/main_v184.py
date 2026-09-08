from __future__ import annotations

from . import main as _base
from .atomic_benchmark_v184 import (
    AtomicExecutionBenchmarkSuiteV184,
    log_atomic_diagnostics_v184,
)
from .config_v184 import SettingsV184
from .dashboard_v18 import DashboardServerV18, DashboardStateV18
from .main_v182 import InactiveTakerExecutor
from .research_context_v184 import PHASE184_RUN_ID
from .winner_research_v184 import WinnerResearchSuiteV184


def cli() -> None:
    # Phase 1.8.4 changes the simulation/shadow maker policy: toxic/stale
    # resting quotes can now be cancelled with an explicit latency race. Live
    # exchange order placement remains unimplemented.
    _base.Settings = SettingsV184
    _base.MakerResearchSuite = WinnerResearchSuiteV184
    _base.ShadowExecutor = InactiveTakerExecutor
    _base.IdealAtomicBenchmarkSuite = AtomicExecutionBenchmarkSuiteV184
    _base.log_atomic_diagnostics = log_atomic_diagnostics_v184
    _base.DashboardState = DashboardStateV18
    _base.DashboardServer = DashboardServerV18

    _ = PHASE184_RUN_ID
    _base.cli()


if __name__ == "__main__":
    cli()
