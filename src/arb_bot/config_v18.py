from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .config import Settings, _bool, _decimal, _decimal_tuple, _int, _str_tuple


@dataclass(frozen=True, slots=True)
class SettingsV18(Settings):
    """Phase 1.8 evidence-gated defaults.

    Phase 1.8 uses its own V18_* environment namespace so a user's existing
    Phase 1.7 .env cannot silently re-enable retired models or restore the old
    5-share/100ms Hybrid settings.
    """

    # Legacy TAKER has not produced a true complete-set win in supplied evidence.
    taker_enabled: bool = field(default_factory=lambda: _bool("V18_TAKER_ENABLED", False))

    # Standard MAKER-99/98/97/96 are retired from the active experiment.
    maker_enabled: bool = field(default_factory=lambda: _bool("V18_MAKER_ENABLED", False))
    maker_variant_targets: tuple[Decimal, ...] = field(
        default_factory=lambda: _decimal_tuple("V18_HYBRID_TARGETS", "0.99,0.98")
    )
    maker_use_empirical_risk_gate: bool = field(
        default_factory=lambda: _bool("V18_EMPIRICAL_RISK_GATE", True)
    )
    maker_empirical_risk_min_samples: int = field(
        default_factory=lambda: _int("V18_EMPIRICAL_RISK_MIN_SAMPLES", 3)
    )

    # HYBRID-99/98 produced true maker+taker complete-set wins. Reduce exposure,
    # complete faster, and cut one-sided inventory sooner.
    hybrid_enabled: bool = field(default_factory=lambda: _bool("V18_HYBRID_ENABLED", True))
    hybrid_trade_shares: Decimal = field(default_factory=lambda: _decimal("V18_HYBRID_TRADE_SHARES", "1"))
    hybrid_min_net_edge_per_share: Decimal = field(
        default_factory=lambda: _decimal("V18_HYBRID_MIN_NET_EDGE_PER_SHARE", "0.005")
    )
    hybrid_completion_latency_ms: int = field(
        default_factory=lambda: _int("V18_HYBRID_COMPLETION_LATENCY_MS", 25)
    )
    hybrid_inventory_timeout_ms: int = field(
        default_factory=lambda: _int("V18_HYBRID_INVENTORY_TIMEOUT_MS", 2500)
    )
    hybrid_max_hold_loss_per_share: Decimal = field(
        default_factory=lambda: _decimal("V18_HYBRID_MAX_HOLD_LOSS_PER_SHARE", "0.010")
    )
    hybrid_min_reprice_interval_ms: int = field(
        default_factory=lambda: _int("V18_HYBRID_MIN_REPRICE_INTERVAL_MS", 100)
    )

    # PMAKER-Q100/Q250 produced true BOTH_MAKER_FILLED wins; Q25/Q50 did not.
    paired_maker_enabled: bool = field(default_factory=lambda: _bool("V18_PMAKER_ENABLED", True))
    paired_maker_trade_shares: Decimal = field(
        default_factory=lambda: _decimal("V18_PMAKER_TRADE_SHARES", "1")
    )
    paired_maker_target_pair: Decimal = field(
        default_factory=lambda: _decimal("V18_PMAKER_TARGET_PAIR", "0.99")
    )
    paired_maker_min_gross_edge_per_share: Decimal = field(
        default_factory=lambda: _decimal("V18_PMAKER_MIN_GROSS_EDGE_PER_SHARE", "0.010")
    )
    paired_maker_max_queues: tuple[Decimal, ...] = field(
        default_factory=lambda: _decimal_tuple("V18_PMAKER_MAX_QUEUES", "100,250")
    )
    paired_maker_max_queue_imbalance: Decimal = field(
        default_factory=lambda: _decimal("V18_PMAKER_MAX_QUEUE_IMBALANCE", "2")
    )

    # All supplied true complete-set wins are ETH. Later evidence can widen this.
    winner_assets: tuple[str, ...] = field(
        default_factory=lambda: _str_tuple("V18_WINNER_ASSETS", "ETH")
    )

    # Winless historical families remain in the repository but are inactive.
    hedge_enabled: bool = field(default_factory=lambda: _bool("V18_HEDGE_ENABLED", False))
    hedge_ghost_enabled: bool = field(default_factory=lambda: _bool("V18_HEDGE_GHOST_ENABLED", False))
    ev_frontier_enabled: bool = field(default_factory=lambda: _bool("V18_EV_ENABLED", False))
    split_sell_enabled: bool = field(default_factory=lambda: _bool("V18_SPLITSELL_ENABLED", False))
    dual_fok_enabled: bool = field(default_factory=lambda: _bool("V18_DFOK_ENABLED", False))
    reverse_dual_fok_enabled: bool = field(
        default_factory=lambda: _bool("V18_RFOK_ENABLED", False)
    )

    # ATOMIC stays on because it is a benchmark ceiling, not executable P&L.
    atomic_benchmark_enabled: bool = field(
        default_factory=lambda: _bool("V18_ATOMIC_BENCHMARK_ENABLED", True)
    )
