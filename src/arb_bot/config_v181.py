from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .config import _bool, _decimal, _int
from .config_v18 import SettingsV18


@dataclass(frozen=True, slots=True)
class SettingsV181(SettingsV18):
    """Phase 1.8.1 out-of-sample selective maker-family experiments."""

    v181_selective_enabled: bool = field(
        default_factory=lambda: _bool("V181_SELECTIVE_ENABLED", True)
    )
    v181_selective_trade_shares: Decimal = field(
        default_factory=lambda: _decimal("V181_SELECTIVE_TRADE_SHARES", "1")
    )

    # HYBRID: historical signal = deeper pair + asymmetric queues.
    v181_hybrid_pair: Decimal = field(
        default_factory=lambda: _decimal("V181_HYBRID_PAIR", "0.97")
    )
    v181_hybrid_small_queue_cap: Decimal = field(
        default_factory=lambda: _decimal("V181_HYBRID_SMALL_QUEUE_CAP", "25")
    )

    # PMAKER / MAKER: historical signal = deeper pair + very small, balanced queues.
    v181_selective_pair_max_imbalance: Decimal = field(
        default_factory=lambda: _decimal("V181_SELECTIVE_PAIR_MAX_IMBALANCE", "2")
    )

    # Cluster correlated outcomes from the same slug so model variants do not
    # masquerade as independent market discoveries.
    v181_episode_window_ms: int = field(
        default_factory=lambda: _int("V181_EPISODE_WINDOW_MS", 2000)
    )

    # Tighten residual maker inventory handling for the selective experiments.
    maker_inventory_timeout_ms: int = field(
        default_factory=lambda: _int("V181_MAKER_INVENTORY_TIMEOUT_MS", 2500)
    )
