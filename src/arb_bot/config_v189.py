from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .config import _bool, _decimal, _int
from .config_v188 import SettingsV188, _int_tuple


@dataclass(frozen=True, slots=True)
class SettingsV189(SettingsV188):
    """Phase 1.8.9 latency-isolation settings.

    The Phase 1.8.8 freshness frontier and full BFOK-RAW strategy lifecycle are
    intentionally disabled by default. Phase 1.8.9 keeps the protected PFOK and
    BFOK controls, adds a controlled 0/1/2/3/5 ms protected latency frontier,
    and replaces BFOK-RAW with a lightweight observation-only probe.
    """

    # Prevent the old hyper-active full RAW engine from being accidentally
    # reintroduced by inherited Phase 1.8.7/1.8.8 settings.
    v187_raw_enabled: bool = field(
        default_factory=lambda: _bool("V187_RAW_ENABLED", False)
    )

    # Freshness was isolated in Phase 1.8.8. Keep the code available but remove
    # the five extra engines from the 1.8.9 hot path unless explicitly enabled.
    v188_freshness_frontier_enabled: bool = field(
        default_factory=lambda: _bool("V188_FRESHNESS_FRONTIER_ENABLED", False)
    )

    v189_latency_frontier_enabled: bool = field(
        default_factory=lambda: _bool("V189_LATENCY_FRONTIER_ENABLED", True)
    )
    v189_latency_targets_ms: tuple[int, ...] = field(
        default_factory=lambda: _int_tuple("V189_LATENCY_TARGETS_MS", "0,1,2,3,5")
    )
    v189_latency_size: Decimal = field(
        default_factory=lambda: _decimal("V189_LATENCY_SIZE", "1")
    )

    v189_raw_observer_enabled: bool = field(
        default_factory=lambda: _bool("V189_RAW_OBSERVER_ENABLED", True)
    )
    v189_raw_observer_rollup_seconds: int = field(
        default_factory=lambda: _int("V189_RAW_OBSERVER_ROLLUP_SECONDS", 5)
    )
