from __future__ import annotations

from . import main as _base
from .atomic_benchmark_v182 import (
    IdealAtomicBenchmarkSuiteV182,
    log_atomic_diagnostics_v182,
)
from .config_v181 import SettingsV181
from .dashboard_v18 import DashboardServerV18, DashboardStateV18
from .simulator import ShadowExecutor
from .winner_research_v182 import WinnerResearchSuiteV182


class InactiveTakerExecutor(ShadowExecutor):
    """Keep diagnostics compatibility while disabling legacy TAKER by default."""

    strategy_name = "TAKER-INACTIVE"

    def can_submit(self, market_id: str) -> bool:
        return False

    def submit(self, opportunity) -> bool:
        return False

    def process_due(self, books):
        return []


def cli() -> None:
    # Reuse stable orchestration while injecting Phase 1.8.2 observational
    # fill-edge and ideal-atomic latency telemetry. Trading behaviour is unchanged.
    _base.Settings = SettingsV181
    _base.MakerResearchSuite = WinnerResearchSuiteV182
    _base.ShadowExecutor = InactiveTakerExecutor
    _base.IdealAtomicBenchmarkSuite = IdealAtomicBenchmarkSuiteV182
    _base.log_atomic_diagnostics = log_atomic_diagnostics_v182
    _base.DashboardState = DashboardStateV18
    _base.DashboardServer = DashboardServerV18
    _base.cli()


if __name__ == "__main__":
    cli()
