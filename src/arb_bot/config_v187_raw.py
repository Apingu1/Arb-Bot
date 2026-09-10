from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .config import _bool, _decimal
from .config_v187 import SettingsV187


@dataclass(frozen=True, slots=True)
class SettingsV187Raw(SettingsV187):
    """Phase 1.8.7 settings with the deliberately ungated BFOK-RAW probe.

    BFOK-RAW is a research upper-bound / stress model, not a deployable live
    execution claim. It removes strategy-level opportunity filters and runs at
    the fastest asynchronous arrival setting supported by this shadow runtime.
    The only remaining requirements are structural: a live two-outcome market,
    ready books, and enough displayed depth to quote both FOK legs.
    """

    v187_raw_enabled: bool = field(
        default_factory=lambda: _bool("V187_RAW_ENABLED", True)
    )
    v187_raw_size: Decimal = field(
        default_factory=lambda: _decimal("V187_RAW_SIZE", "1")
    )
