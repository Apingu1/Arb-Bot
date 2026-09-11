from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal

from .config import _bool, _decimal
from .config_v187_raw import SettingsV187Raw


def _int_tuple(name: str, default: str) -> tuple[int, ...]:
    raw = os.getenv(name, default)
    values = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        values.append(int(part))
    return tuple(values)


@dataclass(frozen=True, slots=True)
class SettingsV188(SettingsV187Raw):
    """Phase 1.8.8 freshness-frontier research settings.

    Existing PFOK/BFOK controls remain unchanged. The new BFOK-FRESH variants
    hold every other protected-strategy parameter constant and vary only the
    maximum acceptable local-book age. This isolates whether the 25 ms ceiling
    is rejecting genuine persistent complete-set arbitrage or stale-book
    illusions.
    """

    v188_freshness_frontier_enabled: bool = field(
        default_factory=lambda: _bool("V188_FRESHNESS_FRONTIER_ENABLED", True)
    )
    v188_freshness_ages_ms: tuple[int, ...] = field(
        default_factory=lambda: _int_tuple("V188_FRESHNESS_AGES_MS", "25,35,50,75,100")
    )
    v188_freshness_size: Decimal = field(
        default_factory=lambda: _decimal("V188_FRESHNESS_SIZE", "1")
    )
