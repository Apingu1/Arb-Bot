from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .config import Settings, _bool, _decimal, _decimal_tuple, _int, _str_tuple


@dataclass(frozen=True, slots=True)
class SettingsV18(Settings):
    """Phase 1.8 strengthened defaults with runtime model×asset control.

    Historical maker-family engines remain instantiated so ARB//TERM can turn
    them on and off at runtime. The UI runtime control matrix is authoritative
    for whether a specific model may start a campaign on a specific asset.
    """

    taker_enabled: bool = field(default_factory=lambda: _bool("V18_TAKER_ENABLED", False))

    # Keep engines internally available; runtime controls determine activity.
    maker_enabled: bool = field(default_factory=lambda: _bool("V18_MAKER_ENGINE_AVAILABLE", True))
    maker_variant_targets: tuple[Decimal, ...] = field(
        default_factory=lambda: _decimal_tuple("V18_HYBRID_TARGETS", "0.99,0.98,0.97,0.96")
    )
    maker_use_empirical_risk_gate: bool = field(
        default_factory=lambda: _bool("V18_EMPIRICAL_RISK_GATE", True)
    )
    maker_empirical_risk_min_samples: int = field(
        default_factory=lambda: _int("V18_EMPIRICAL_RISK_MIN_SAMPLES", 3)
    )

    hybrid_enabled: bool = field(default_factory=lambda: _bool("V18_HYBRID_ENGINE_AVAILABLE", True))
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

    paired_maker_enabled: bool = field(default_factory=lambda: _bool("V18_PMAKER_ENGINE_AVAILABLE", True))
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
        default_factory=lambda: _decimal_tuple("V18_PMAKER_MAX_QUEUES", "25,50,100,250")
    )
    paired_maker_max_queue_imbalance: Decimal = field(
        default_factory=lambda: _decimal("V18_PMAKER_MAX_QUEUE_IMBALANCE", "2")
    )

    # Kept for backward compatibility; runtime controls now decide exact assets.
    winner_assets: tuple[str, ...] = field(
        default_factory=lambda: _str_tuple("V18_WINNER_ASSETS", "BTC,ETH,BNB,SOL")
    )

    # Non-maker historical families remain inactive in this UI-control pass.
    hedge_enabled: bool = field(default_factory=lambda: _bool("V18_HEDGE_ENABLED", False))
    hedge_ghost_enabled: bool = field(default_factory=lambda: _bool("V18_HEDGE_GHOST_ENABLED", False))
    ev_frontier_enabled: bool = field(default_factory=lambda: _bool("V18_EV_ENABLED", False))
    split_sell_enabled: bool = field(default_factory=lambda: _bool("V18_SPLITSELL_ENABLED", False))
    dual_fok_enabled: bool = field(default_factory=lambda: _bool("V18_DFOK_ENABLED", False))
    reverse_dual_fok_enabled: bool = field(
        default_factory=lambda: _bool("V18_RFOK_ENABLED", False)
    )

    atomic_benchmark_enabled: bool = field(
        default_factory=lambda: _bool("V18_ATOMIC_BENCHMARK_ENABLED", True)
    )
