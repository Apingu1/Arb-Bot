from __future__ import annotations

from . import main as _base
from .config_v18 import SettingsV18
from .dashboard_v18 import DashboardServerV18, DashboardStateV18
from .simulator import ShadowExecutor
from .winner_research_v18 import WinnerResearchSuiteV18


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
    # Reuse stable Phase 1.7 orchestration but inject Phase 1.8 settings,
    # runtime-controlled maker research and the writable ARB//TERM dashboard.
    _base.Settings = SettingsV18
    _base.MakerResearchSuite = WinnerResearchSuiteV18
    _base.ShadowExecutor = InactiveTakerExecutor
    _base.DashboardState = DashboardStateV18
    _base.DashboardServer = DashboardServerV18
    _base.cli()


if __name__ == "__main__":
    cli()
