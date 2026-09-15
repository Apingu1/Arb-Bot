from __future__ import annotations

import os
from dataclasses import dataclass, field

from .config import _int
from .config_v189 import SettingsV189


@dataclass(frozen=True, slots=True)
class SettingsV1810(SettingsV189):
    """Phase 1.8.10 surge-concentration and capacity diagnostics.

    All protected PFOK/BFOK execution settings are inherited unchanged.  New
    settings control reporting/archival only and cannot loosen an entry gate.
    """

    v1810_episode_window_ms: int = field(
        default_factory=lambda: _int("V1810_EPISODE_WINDOW_MS", 500)
    )
    v1810_session_archive_dir: str = field(
        default_factory=lambda: os.getenv(
            "V1810_SESSION_ARCHIVE_DIR", "data/phase1810_sessions"
        )
    )
