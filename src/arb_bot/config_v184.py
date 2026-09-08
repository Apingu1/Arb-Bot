from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .config import _bool, _decimal, _int, _int_tuple
from .config_v181 import SettingsV181


@dataclass(frozen=True, slots=True)
class SettingsV184(SettingsV181):
    """Phase 1.8.4 fast adverse-selection controls.

    These settings alter only the simulation/shadow maker research path. Live
    exchange order placement is still not implemented by this repository.
    """

    # Do not place a selective resting order when the current maker-first / 
    # taker-second economics are already as toxic as the cancellation boundary.
    v184_placement_edge_gate_enabled: bool = field(
        default_factory=lambda: _bool("V184_PLACEMENT_EDGE_GATE_ENABLED", True)
    )
    v184_placement_min_edge_per_share: Decimal = field(
        default_factory=lambda: _decimal("V184_PLACEMENT_MIN_EDGE_PER_SHARE", "-0.005")
    )

    v184_fast_cancel_enabled: bool = field(
        default_factory=lambda: _bool("V184_FAST_CANCEL_ENABLED", True)
    )
    v184_prefill_cancel_edge_per_share: Decimal = field(
        default_factory=lambda: _decimal("V184_PREFILL_CANCEL_EDGE_PER_SHARE", "-0.005")
    )
    v184_stale_quote_age_ms: int = field(
        default_factory=lambda: _int("V184_STALE_QUOTE_AGE_MS", 500)
    )
    v184_stale_max_edge_per_share: Decimal = field(
        default_factory=lambda: _decimal("V184_STALE_MAX_EDGE_PER_SHARE", "0")
    )
    v184_hard_quote_age_ms: int = field(
        default_factory=lambda: _int("V184_HARD_QUOTE_AGE_MS", 1000)
    )
    v184_cancel_latency_ms: int = field(
        default_factory=lambda: _int("V184_CANCEL_LATENCY_MS", 5)
    )
    v184_max_opposite_book_age_ms: int = field(
        default_factory=lambda: _int("V184_MAX_OPPOSITE_BOOK_AGE_MS", 250)
    )
    v184_prefill_history_max_samples: int = field(
        default_factory=lambda: _int("V184_PREFILL_HISTORY_MAX_SAMPLES", 512)
    )

    # The legacy maker base waited 500 ms after every cancellation. Once the
    # placement edge gate is active, 50 ms is sufficient to avoid immediately
    # recycling a rejected/stale quote while allowing much faster re-entry when
    # the book genuinely becomes attractive again.
    maker_requote_cooldown_ms: int = field(
        default_factory=lambda: _int("V184_MAKER_REQUOTE_COOLDOWN_MS", 50)
    )

    # Make the post-first-fill shadow actions materially faster so the report
    # measures a realistic low-latency frontier rather than the old 25/100 ms
    # research defaults.
    hybrid_completion_latency_ms: int = field(
        default_factory=lambda: _int("V184_HYBRID_COMPLETION_LATENCY_MS", 5)
    )
    hybrid_min_reprice_interval_ms: int = field(
        default_factory=lambda: _int("V184_HYBRID_MIN_REPRICE_INTERVAL_MS", 25)
    )
    hybrid_inventory_timeout_ms: int = field(
        default_factory=lambda: _int("V184_HYBRID_INVENTORY_TIMEOUT_MS", 500)
    )
    maker_inventory_timeout_ms: int = field(
        default_factory=lambda: _int("V184_MAKER_INVENTORY_TIMEOUT_MS", 750)
    )

    # The event-driven path already runs on every exchange update; keep a 1 ms
    # timer as the fallback for cancellation deadlines and atomic replays.
    strategy_timer_interval_ms: int = field(
        default_factory=lambda: _int("V184_STRATEGY_TIMER_INTERVAL_MS", 1)
    )

    # Increasingly realistic atomic shadow execution checkpoints. These still
    # use local order books and do not claim exchange-side fill certainty.
    v184_atomic_execution_latencies_ms: tuple[int, ...] = field(
        default_factory=lambda: _int_tuple("V184_ATOMIC_EXECUTION_LATENCIES_MS", "2,5,10")
    )
    v184_atomic_min_execution_edge_per_share: Decimal = field(
        default_factory=lambda: _decimal("V184_ATOMIC_MIN_EXECUTION_EDGE_PER_SHARE", "0.0001")
    )
    v184_atomic_max_book_age_ms: int = field(
        default_factory=lambda: _int("V184_ATOMIC_MAX_BOOK_AGE_MS", 25)
    )
