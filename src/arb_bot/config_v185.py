from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .config import _bool, _decimal, _decimal_tuple, _int
from .config_v184 import SettingsV184


@dataclass(frozen=True, slots=True)
class SettingsV185(SettingsV184):
    """Phase 1.8.5 profit-first shadow execution controls.

    PFOK only books P&L after an executable two-leg attempt or an honest
    one-leg recovery. Rejected opportunities remain no-trades. The defaults
    below were widened after the initial 1.8.5 run showed that a 10 ms dual-book
    freshness gate + 1.5 cent edge + 3x depth suppressed every candidate.
    """

    v181_selective_enabled: bool = field(
        default_factory=lambda: _bool("V185_KEEP_SELECTIVE_MAKER_RESEARCH", False)
    )
    atomic_reverse_enabled: bool = field(
        default_factory=lambda: _bool("V185_ATOMIC_REVERSE_CONTROL", False)
    )

    v185_profit_fok_enabled: bool = field(
        default_factory=lambda: _bool("V185_PROFIT_FOK_ENABLED", True)
    )
    v185_profit_sizes: tuple[Decimal, ...] = field(
        default_factory=lambda: _decimal_tuple("V185_PROFIT_SIZES", "1,2,5")
    )

    # Detection remains positive after taker fees, but no longer requires the
    # unusually large +1.5 cent edge that only appeared in rare atomic episodes.
    v185_detection_min_edge_per_share: Decimal = field(
        default_factory=lambda: _decimal("V185_DETECTION_MIN_EDGE_PER_SHARE", "0.005")
    )
    # Re-quote after simulated end-to-end latency. If +0.003/share does not
    # survive, no first-leg shadow fill is permitted.
    v185_preflight_min_edge_per_share: Decimal = field(
        default_factory=lambda: _decimal("V185_PREFLIGHT_MIN_EDGE_PER_SHARE", "0.003")
    )
    # The complete-set result must remain positive after both taker fees.
    v185_final_min_edge_per_share: Decimal = field(
        default_factory=lambda: _decimal("V185_FINAL_MIN_EDGE_PER_SHARE", "0.001")
    )

    # Require real depth, but avoid the original 3x filter that eliminated all
    # otherwise executable candidates.
    v185_detection_coverage_multiple: Decimal = field(
        default_factory=lambda: _decimal("V185_DETECTION_COVERAGE_MULTIPLE", "1.5")
    )
    v185_preflight_coverage_multiple: Decimal = field(
        default_factory=lambda: _decimal("V185_PREFLIGHT_COVERAGE_MULTIPLE", "1.0")
    )

    # Websocket updates arrive per token/book. A 10 ms requirement on both
    # books was too strict because the opposite token may simply not have had an
    # update in the same 10 ms window. 25 ms is still much tighter than the old
    # 100/250 ms research gates and remains below the observed 25 ms expiry
    # frontier for the strongest opportunities.
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

    # Gate snapshots are sampled rather than logged on every websocket message,
    # so we can diagnose zero-trade runs without recreating multi-million-line
    # reject logs.
    v185_gate_sample_interval_ms: int = field(
        default_factory=lambda: _int("V185_GATE_SAMPLE_INTERVAL_MS", 250)
    )
