from __future__ import annotations

from . import main as _base
from .config_v18 import SettingsV18
from .simulator import ShadowExecutor
from .winner_research_v18 import WinnerResearchSuiteV18


class InactiveTakerExecutor(ShadowExecutor):
    """Keep diagnostics compatibility while disabling the winless legacy TAKER."""

    strategy_name = "TAKER-INACTIVE"

    def can_submit(self, market_id: str) -> bool:
        return False

    def submit(self, opportunity) -> bool:
        return False

    def process_due(self, books):
        return []


def cli() -> None:
    # Reuse the stable Phase 1.7 orchestration while replacing only the runtime
    # configuration and maker research suite. This keeps discovery/dashboard/
    # recording behavior identical to the accepted 1.7 experiment surface.
    _base.Settings = SettingsV18
    _base.MakerResearchSuite = WinnerResearchSuiteV18
    _base.ShadowExecutor = InactiveTakerExecutor
    _base.cli()


if __name__ == "__main__":
    cli()
