from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .config import Settings, _bool, _decimal, _decimal_tuple, _int, _str_tuple


@dataclass(frozen=True, slots=True)
class SettingsV18(Settings):
    """Phase 1.8 evidence-gated defaults.

    Only models with observed true complete-set wins remain active by default.
    Historical controls stay available through environment overrides.
    """

    # Legacy TAKER has not produced a true win in the supplied Phase 1.7 logs.
    taker_enabled: bool = field(default_factory=lambda: _bool("TAKER_SHADOW_ENABLED", False))

    # Standard MAKER-99/98/97/96: zero completed pairs in the supplied evidence.
    maker_enabled: bool = field(default_factory=lambda: _bool("MAKER_SHADOW_ENABLED", False))
    maker_variant_targets: tuple[Decimal, ...] = field(
        default_factory=lambda: _decimal_tuple("MAKER_VARIANT_TARGETS", "0.99,0.98")
    )
    maker_use_empirical_risk_gate: bool = field(
        default_factory=lambda: _bool("MAKER_USE_EMPIRICAL_RISK_GATE", True)
    )
    maker_empirical_risk_min_samples: int = field(
        default_factory=lambda: _int("MAKER_EMPIRICAL_RISK_MIN_SAMPLES", 3)
    )

    # HYBRID-99/98 produced true maker+taker complete-set wins. Reduce exposure,
    # complete faster, and cut one-sided inventory sooner.
    hybrid_enabled: bool = field(default_factory=lambda: _bool("HYBRID_SHADOW_ENABLED", True))
    hybrid_trade_shares: Decimal = field(default_factory=lambda: _decimal("HYBRID_TRADE_SHARES", "1"))
    hybrid_min_net_edge_per_share: Decimal = field(
        default_factory=lambda: _decimal("HYBRID_MIN_NET_EDGE_PER_SHARE", "0.005")
    )
    hybrid_completion_latency_ms: int = field(
        default_factory=lambda: _int("HYBRID_COMPLETION_LATENCY_MS", 25)
    )
    hybrid_inventory_timeout_ms: int = field(
        default_factory=lambda: _int("HYBRID_INVENTORY_TIMEOUT_MS", 2500)
    )
    hybrid_max_hold_loss_per_share: Decimal = field(
        default_factory=lambda: _decimal("HYBRID_MAX_HOLD_LOSS_PER_SHARE", "0.010")
    )
    hybrid_min_reprice_interval_ms: int = field(
        default_factory=lambda: _int("HYBRID_MIN_REPRICE_INTERVAL_MS", 100)
    )

    # PMAKER-Q100/Q250 produced true BOTH_MAKER_FILLED wins; Q25/Q50 did not.
    paired_maker_enabled: bool = field(default_factory=lambda: _bool("PAIRED_MAKER_ENABLED", True))
    paired_maker_trade_shares: Decimal = field(
        default_factory=lambda: _decimal("PAIRED_MAKER_TRADE_SHARES", "1")
    )
    paired_maker_target_pair: Decimal = field(
        default_factory=lambda: _decimal("PAIRED_MAKER_TARGET_PAIR", "0.99")
    )
    paired_maker_min_gross_edge_per_share: Decimal = field(
        default_factory=lambda: _decimal("PAIRED_MAKER_MIN_GROSS_EDGE_PER_SHARE", "0.010")
    )
    paired_maker_max_queues: tuple[Decimal, ...] = field(
        default_factory=lambda: _decimal_tuple("PAIRED_MAKER_MAX_QUEUES", "100,250")
    )
    paired_maker_max_queue_imbalance: Decimal = field(
        default_factory=lambda: _decimal("PAIRED_MAKER_MAX_QUEUE_IMBALANCE", "2")
    )

    # The supplied true wins are all ETH. This is deliberately configurable so
    # later evidence can widen the universe without restoring the losing spray.
    winner_assets: tuple[str, ...] = field(
        default_factory=lambda: _str_tuple("WINNER_ASSETS", "ETH")
    )

    # Winless historical research families are inactive by default in 1.8.
    hedge_enabled: bool = field(default_factory=lambda: _bool("HEDGE_SHADOW_ENABLED", False))
    hedge_ghost_enabled: bool = field(default_factory=lambda: _bool("HEDGE_GHOST_ENABLED", False))
    ev_frontier_enabled: bool = field(default_factory=lambda: _bool("EV_FRONTIER_ENABLED", False))
    split_sell_enabled: bool = field(default_factory=lambda: _bool("SPLIT_SELL_ENABLED", False))
    dual_fok_enabled: bool = field(default_factory=lambda: _bool("DUAL_FOK_ENABLED", False))
    reverse_dual_fok_enabled: bool = field(
        default_factory=lambda: _bool("REVERSE_DUAL_FOK_ENABLED", False)
    )

    # ATOMIC remains on because it is a benchmark ceiling, not executable P&L.
    atomic_benchmark_enabled: bool = field(
        default_factory=lambda: _bool("ATOMIC_BENCHMARK_ENABLED", True)
    )
