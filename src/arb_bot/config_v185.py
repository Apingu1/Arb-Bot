from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .config import _bool, _decimal, _decimal_tuple, _int
from .config_v184 import SettingsV184


@dataclass(frozen=True, slots=True)
class SettingsV185(SettingsV184):
    """Phase 1.8.5 profit-first shadow execution controls.

    PFOK only books P&L after an executable two-leg attempt or an honest
    one-leg recovery. Rejected opportunities remain no-trades. The control
    PFOK settings below are intentionally unchanged from the first profitable
    Phase 1.8.5 run. Parallel frontier variants are enabled separately and do
    not alter control-PFOK behaviour.
    """

    v181_selective_enabled: bool = field(
        default_factory=lambda: _bool("V185_KEEP_SELECTIVE_MAKER_RESEARCH", False)
    )
    atomic_reverse_enabled: bool = field(
        default_factory=lambda: _bool("V185_ATOMIC_REVERSE_CONTROL", False)
    )

    # ------------------------------------------------------------------
    # CONTROL PFOK -- DO NOT TUNE VIA PARALLEL-FRONTIER CODE.
    # ------------------------------------------------------------------
    v185_profit_fok_enabled: bool = field(
        default_factory=lambda: _bool("V185_PROFIT_FOK_ENABLED", True)
    )
    v185_profit_sizes: tuple[Decimal, ...] = field(
        default_factory=lambda: _decimal_tuple("V185_PROFIT_SIZES", "1,2,5")
    )
    v185_detection_min_edge_per_share: Decimal = field(
        default_factory=lambda: _decimal("V185_DETECTION_MIN_EDGE_PER_SHARE", "0.005")
    )
    v185_preflight_min_edge_per_share: Decimal = field(
        default_factory=lambda: _decimal("V185_PREFLIGHT_MIN_EDGE_PER_SHARE", "0.003")
    )
    v185_final_min_edge_per_share: Decimal = field(
        default_factory=lambda: _decimal("V185_FINAL_MIN_EDGE_PER_SHARE", "0.001")
    )
    v185_detection_coverage_multiple: Decimal = field(
        default_factory=lambda: _decimal("V185_DETECTION_COVERAGE_MULTIPLE", "1.5")
    )
    v185_preflight_coverage_multiple: Decimal = field(
        default_factory=lambda: _decimal("V185_PREFLIGHT_COVERAGE_MULTIPLE", "1.0")
    )
    v185_max_book_age_ms: int = field(
        default_factory=lambda: _int("V185_MAX_BOOK_AGE_MS", 25)
    )
    v185_base_latency_ms: int = field(
        default_factory=lambda: _int("V185_BASE_LATENCY_MS", 2)
    )
    v185_leg_gap_ms: int = field(
        default_factory=lambda: _int("V185_LEG_GAP_MS", 1)
    )
    v185_recovery_latency_ms: int = field(
        default_factory=lambda: _int("V185_RECOVERY_LATENCY_MS", 2)
    )
    v185_cooldown_ms: int = field(
        default_factory=lambda: _int("V185_COOLDOWN_MS", 250)
    )
    v185_use_surge_gate: bool = field(
        default_factory=lambda: _bool("V185_USE_SURGE_GATE", True)
    )
    v185_gate_sample_interval_ms: int = field(
        default_factory=lambda: _int("V185_GATE_SAMPLE_INTERVAL_MS", 250)
    )

    # ------------------------------------------------------------------
    # PARALLEL PFOK FRONTIER.
    # These variants run against the same feed with the same honest execution
    # and recovery mechanics as PFOK. They exist to measure opportunity cost
    # from edge, depth, surge and size filters without modifying the control.
    # ------------------------------------------------------------------
    v185_parallel_pfok_enabled: bool = field(
        default_factory=lambda: _bool("V185_PARALLEL_PFOK_ENABLED", True)
    )
    # Experimental variants sample gates less often than the control to avoid
    # recreating the multi-million-line telemetry problem.
    v185_frontier_gate_sample_interval_ms: int = field(
        default_factory=lambda: _int("V185_FRONTIER_GATE_SAMPLE_INTERVAL_MS", 1000)
    )
    # Report-side clustering window for correlated variants firing on the same
    # market dislocation. This does not affect execution.
    v185_episode_window_ms: int = field(
        default_factory=lambda: _int("V185_EPISODE_WINDOW_MS", 500)
    )
