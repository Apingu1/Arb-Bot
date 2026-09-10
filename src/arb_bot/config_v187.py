from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .config import _bool, _decimal, _decimal_tuple, _int
from .config_v186 import SettingsV186


@dataclass(frozen=True, slots=True)
class SettingsV187(SettingsV186):
    """Phase 1.8.7 parallel batch-FOK research settings.

    PFOK, PFOK-S10 and PFOK-S20 remain as unchanged sequential controls.
    The new BFOK family models two FOK orders constructed from one detection
    snapshot and arriving at the venue together, with independent per-leg FOK
    validation. It is deliberately not treated as atomic: either leg may miss
    and recovery P&L is booked honestly.
    """

    v187_batch_fok_enabled: bool = field(
        default_factory=lambda: _bool("V187_BATCH_FOK_ENABLED", True)
    )
    v187_batch_sizes: tuple[Decimal, ...] = field(
        default_factory=lambda: _decimal_tuple("V187_BATCH_SIZES", "1,5,10,20")
    )
    v187_detection_min_edge_per_share: Decimal = field(
        default_factory=lambda: _decimal("V187_DETECTION_MIN_EDGE_PER_SHARE", "0.005")
    )
    v187_final_min_edge_per_share: Decimal = field(
        default_factory=lambda: _decimal("V187_FINAL_MIN_EDGE_PER_SHARE", "0.001")
    )
    v187_detection_coverage_multiple: Decimal = field(
        default_factory=lambda: _decimal("V187_DETECTION_COVERAGE_MULTIPLE", "1.5")
    )
    v187_max_book_age_ms: int = field(
        default_factory=lambda: _int("V187_MAX_BOOK_AGE_MS", 25)
    )
    v187_batch_arrival_latency_ms: int = field(
        default_factory=lambda: _int("V187_BATCH_ARRIVAL_LATENCY_MS", 1)
    )
    v187_recovery_latency_ms: int = field(
        default_factory=lambda: _int("V187_RECOVERY_LATENCY_MS", 1)
    )
    v187_cooldown_ms: int = field(
        default_factory=lambda: _int("V187_COOLDOWN_MS", 250)
    )
    v187_use_surge_gate: bool = field(
        default_factory=lambda: _bool("V187_USE_SURGE_GATE", True)
    )

    # Risk-adjusted BFOK-EV. The four fixed-size BFOK controls gather outcome
    # observations into size/asset/edge buckets. BFOK-EV uses a smoothed prior
    # until enough same-bucket observations exist, then increasingly relies on
    # observed both-fill probability and one-leg recovery loss.
    v187_ev_enabled: bool = field(
        default_factory=lambda: _bool("V187_EV_ENABLED", True)
    )
    v187_ev_sizes: tuple[Decimal, ...] = field(
        default_factory=lambda: _decimal_tuple("V187_EV_SIZES", "1,5,10,20")
    )
    v187_ev_edge_bands: tuple[Decimal, ...] = field(
        default_factory=lambda: _decimal_tuple("V187_EV_EDGE_BANDS", "0.005,0.010,0.020,0.050")
    )
    v187_ev_prior_both_probability: Decimal = field(
        default_factory=lambda: _decimal("V187_EV_PRIOR_BOTH_PROBABILITY", "0.80")
    )
    v187_ev_prior_weight: Decimal = field(
        default_factory=lambda: _decimal("V187_EV_PRIOR_WEIGHT", "8")
    )
    v187_ev_prior_miss_loss_per_share: Decimal = field(
        default_factory=lambda: _decimal("V187_EV_PRIOR_MISS_LOSS_PER_SHARE", "0.015")
    )
    v187_ev_min_expected_pnl: Decimal = field(
        default_factory=lambda: _decimal("V187_EV_MIN_EXPECTED_PNL", "0.001")
    )
    v187_ev_min_empirical_samples: int = field(
        default_factory=lambda: _int("V187_EV_MIN_EMPIRICAL_SAMPLES", 12)
    )

    # Optional inventory-backed reverse complete-set research. Kept OFF by
    # default because it assumes pre-positioned complete-set inventory and is a
    # separate capital model from BUY-pair PFOK/BFOK.
    v187_inventory_enabled: bool = field(
        default_factory=lambda: _bool("V187_INVENTORY_ENABLED", False)
    )
    v187_inventory_size: Decimal = field(
        default_factory=lambda: _decimal("V187_INVENTORY_SIZE", "5")
    )
    v187_inventory_min_edge_per_share: Decimal = field(
        default_factory=lambda: _decimal("V187_INVENTORY_MIN_EDGE_PER_SHARE", "0.005")
    )

    # Preserve only the sequential PFOK controls explicitly required for this
    # phase. The older EDGE3/DEPTH1/NOSURGE/AGGR/FAST research remains available
    # unchanged on the Phase 1.8.6 branch.
    v187_keep_pfok_size_controls: bool = field(
        default_factory=lambda: _bool("V187_KEEP_PFOK_SIZE_CONTROLS", True)
    )
