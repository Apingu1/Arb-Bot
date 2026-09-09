from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .config import _bool, _decimal, _decimal_tuple, _int
from .config_v184 import SettingsV184


@dataclass(frozen=True, slots=True)
class SettingsV185(SettingsV184):
    """Phase 1.8.5 profit-first shadow execution controls.

    The objective is no longer to manufacture activity from weak maker setups.
    A single canonical BUY complete-set strategy is allowed to submit only when
    the local books show a robust positive edge, sufficient depth, fresh books,
    and the opportunity survives a second pre-flight check after simulated
    end-to-end latency. One-leg misses are still realized honestly through the
    existing recovery logic rather than being discarded.
    """

    # Phase 1.8.4 selective maker research generated very large reject logs and
    # no useful fills. Keep it available by env override, but off by default in
    # this profit-focused branch.
    v181_selective_enabled: bool = field(
        default_factory=lambda: _bool("V185_KEEP_SELECTIVE_MAKER_RESEARCH", False)
    )

    # SELL_PAIR atomic rows mirror BUY_PAIR unless complete-set inventory is
    # genuinely pre-positioned. Disable the mirrored control by default so the
    # report represents independent opportunities cleanly.
    atomic_reverse_enabled: bool = field(
        default_factory=lambda: _bool("V185_ATOMIC_REVERSE_CONTROL", False)
    )

    v185_profit_fok_enabled: bool = field(
        default_factory=lambda: _bool("V185_PROFIT_FOK_ENABLED", True)
    )
    v185_profit_sizes: tuple[Decimal, ...] = field(
        default_factory=lambda: _decimal_tuple("V185_PROFIT_SIZES", "1,2,5")
    )

    # Require a meaningful detection buffer. The last observed executable
    # atomic episode carried roughly +0.026 to +0.036/share before decay, so a
    # +0.015/share trigger deliberately ignores marginal opportunities.
    v185_detection_min_edge_per_share: Decimal = field(
        default_factory=lambda: _decimal("V185_DETECTION_MIN_EDGE_PER_SHARE", "0.015")
    )
    # The pair is re-quoted after end-to-end latency before any first-leg shadow
    # fill is permitted. If less than +0.010/share survives, no trade is sent.
    v185_preflight_min_edge_per_share: Decimal = field(
        default_factory=lambda: _decimal("V185_PREFLIGHT_MIN_EDGE_PER_SHARE", "0.010")
    )
    # Before the second FOK leg is accepted, the complete-set result must still
    # clear this floor after both taker fees. Otherwise the strategy takes the
    # one-leg recovery path instead of pretending the second leg filled.
    v185_final_min_edge_per_share: Decimal = field(
        default_factory=lambda: _decimal("V185_FINAL_MIN_EDGE_PER_SHARE", "0.005")
    )

    # Robustness against disappearing top-of-book liquidity.
    v185_detection_coverage_multiple: Decimal = field(
        default_factory=lambda: _decimal("V185_DETECTION_COVERAGE_MULTIPLE", "3")
    )
    v185_preflight_coverage_multiple: Decimal = field(
        default_factory=lambda: _decimal("V185_PREFLIGHT_COVERAGE_MULTIPLE", "1.5")
    )
    v185_max_book_age_ms: int = field(
        default_factory=lambda: _int("V185_MAX_BOOK_AGE_MS", 10)
    )

    # Simulated order path. The observed scheduler may execute later than these
    # targets; actual elapsed times are recorded in every finalized trade.
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
