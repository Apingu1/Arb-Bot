from __future__ import annotations

from . import main as _base
from .atomic_benchmark_v183 import (
    IdealAtomicBenchmarkSuiteV183,
    log_atomic_diagnostics_v183,
)
from .config_v181 import SettingsV181
from .dashboard_v18 import DashboardServerV18, DashboardStateV18
from .main_v182 import InactiveTakerExecutor
from .research_context_v183 import PHASE183_RUN_ID
from .winner_research_v183 import WinnerResearchSuiteV183


def cli() -> None:
    # Phase 1.8.3 remains observational. No execution threshold or strategy
    # action is changed; only the research suites/reporting are replaced.
    _base.Settings = SettingsV181
    _base.MakerResearchSuite = WinnerResearchSuiteV183
    _base.ShadowExecutor = InactiveTakerExecutor
    _base.IdealAtomicBenchmarkSuite = IdealAtomicBenchmarkSuiteV183
    _base.log_atomic_diagnostics = log_atomic_diagnostics_v183
    _base.DashboardState = DashboardStateV18
    _base.DashboardServer = DashboardServerV18

    # Keep the process-wide run id import alive and discoverable for debugging.
    _ = PHASE183_RUN_ID
    _base.cli()


if __name__ == "__main__":
    cli()
