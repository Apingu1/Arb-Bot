from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


def _decimal(name: str, default: str) -> Decimal:
    return Decimal(_env(name, default))


def _int(name: str, default: int) -> int:
    return int(_env(name, str(default)))


def _bool(name: str, default: bool) -> bool:
    raw = _env(name, "true" if default else "false").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _decimal_tuple(name: str, default: str) -> tuple[Decimal, ...]:
    raw = _env(name, default)
    return tuple(Decimal(part.strip()) for part in raw.split(",") if part.strip())


def _int_tuple(name: str, default: str) -> tuple[int, ...]:
    raw = _env(name, default)
    return tuple(int(part.strip()) for part in raw.split(",") if part.strip())


@dataclass(frozen=True, slots=True)
class Settings:
    market_query: str = field(default_factory=lambda: _env("MARKET_QUERY", "BTC Up or Down 15m"))
    min_net_edge_per_share: Decimal = field(default_factory=lambda: _decimal("MIN_NET_EDGE_PER_SHARE", "0.005"))
    min_expected_profit_usdc: Decimal = field(default_factory=lambda: _decimal("MIN_EXPECTED_PROFIT_USDC", "0.10"))
    min_trade_shares: Decimal = field(default_factory=lambda: _decimal("MIN_TRADE_SHARES", "5"))
    max_trade_shares: Decimal = field(default_factory=lambda: _decimal("MAX_TRADE_SHARES", "100"))
    risk_buffer_per_share: Decimal = field(default_factory=lambda: _decimal("RISK_BUFFER_PER_SHARE", "0.002"))
    recovery_penalty_per_share: Decimal = field(default_factory=lambda: _decimal("RECOVERY_PENALTY_PER_SHARE", "0.002"))
    shadow_latency_ms: int = field(default_factory=lambda: _int("SHADOW_LATENCY_MS", 200))
    shadow_recovery_latency_ms: int = field(default_factory=lambda: _int("SHADOW_RECOVERY_LATENCY_MS", 100))
    market_cooldown_ms: int = field(default_factory=lambda: _int("MARKET_COOLDOWN_MS", 1000))
    max_book_age_ms: int = field(default_factory=lambda: _int("MAX_BOOK_AGE_MS", 1500))
    market_refresh_seconds: int = field(default_factory=lambda: _int("MARKET_REFRESH_SECONDS", 60))
    diagnostic_interval_seconds: int = field(default_factory=lambda: _int("DIAGNOSTIC_INTERVAL_SECONDS", 10))
    edge_record_min_interval_ms: int = field(default_factory=lambda: _int("EDGE_RECORD_MIN_INTERVAL_MS", 0))

    # Phase 1.3 queue-aware maker benchmarks remain active as controls.
    maker_enabled: bool = field(default_factory=lambda: _bool("MAKER_SHADOW_ENABLED", True))
    maker_trade_shares: Decimal = field(default_factory=lambda: _decimal("MAKER_TRADE_SHARES", "5"))
    maker_variant_targets: tuple[Decimal, ...] = field(
        default_factory=lambda: _decimal_tuple("MAKER_VARIANT_TARGETS", "0.99,0.98,0.97,0.96")
    )
    maker_tick_size: Decimal = field(default_factory=lambda: _decimal("MAKER_TICK_SIZE", "0.01"))
    maker_min_gross_edge_per_share: Decimal = field(default_factory=lambda: _decimal("MAKER_MIN_GROSS_EDGE_PER_SHARE", "0.005"))
    maker_order_ttl_ms: int = field(default_factory=lambda: _int("MAKER_ORDER_TTL_MS", 1500))
    maker_max_quote_age_ms: int = field(default_factory=lambda: _int("MAKER_MAX_QUOTE_AGE_MS", 30000))
    maker_reprice_ticks: int = field(default_factory=lambda: _int("MAKER_REPRICE_TICKS", 2))
    maker_requote_cooldown_ms: int = field(default_factory=lambda: _int("MAKER_REQUOTE_COOLDOWN_MS", 500))
    maker_inventory_timeout_ms: int = field(default_factory=lambda: _int("MAKER_INVENTORY_TIMEOUT_MS", 5000))
    maker_min_seconds_to_expiry: int = field(default_factory=lambda: _int("MAKER_MIN_SECONDS_TO_EXPIRY", 30))
    maker_use_empirical_risk_gate: bool = field(default_factory=lambda: _bool("MAKER_USE_EMPIRICAL_RISK_GATE", False))
    maker_empirical_risk_min_samples: int = field(default_factory=lambda: _int("MAKER_EMPIRICAL_RISK_MIN_SAMPLES", 20))

    hybrid_enabled: bool = field(default_factory=lambda: _bool("HYBRID_SHADOW_ENABLED", True))
    hybrid_trade_shares: Decimal = field(default_factory=lambda: _decimal("HYBRID_TRADE_SHARES", "5"))
    hybrid_min_net_edge_per_share: Decimal = field(default_factory=lambda: _decimal("HYBRID_MIN_NET_EDGE_PER_SHARE", "0.003"))
    hybrid_completion_latency_ms: int = field(default_factory=lambda: _int("HYBRID_COMPLETION_LATENCY_MS", 100))
    hybrid_inventory_timeout_ms: int = field(default_factory=lambda: _int("HYBRID_INVENTORY_TIMEOUT_MS", 5000))
    hybrid_max_hold_loss_per_share: Decimal = field(default_factory=lambda: _decimal("HYBRID_MAX_HOLD_LOSS_PER_SHARE", "0.02"))
    hybrid_min_reprice_interval_ms: int = field(default_factory=lambda: _int("HYBRID_MIN_REPRICE_INTERVAL_MS", 500))

    # Shared SURGE / toxic-flow gate.
    surge_move_1s: Decimal = field(default_factory=lambda: _decimal("SURGE_MOVE_1S", "0.04"))
    surge_move_3s: Decimal = field(default_factory=lambda: _decimal("SURGE_MOVE_3S", "0.08"))
    surge_updates_per_second: int = field(default_factory=lambda: _int("SURGE_UPDATES_PER_SECOND", 300))
    surge_pause_ms: int = field(default_factory=lambda: _int("SURGE_PAUSE_MS", 1500))
    surge_one_sided_window_seconds: int = field(default_factory=lambda: _int("SURGE_ONE_SIDED_WINDOW_SECONDS", 30))
    surge_one_sided_count: int = field(default_factory=lambda: _int("SURGE_ONE_SIDED_COUNT", 3))

    # Phase 1.4 hedgeability-first maker -> taker research.
    hedge_enabled: bool = field(default_factory=lambda: _bool("HEDGE_SHADOW_ENABLED", True))
    hedge_net_edge_targets: tuple[Decimal, ...] = field(
        default_factory=lambda: _decimal_tuple("HEDGE_NET_EDGE_TARGETS", "0.005,0.010,0.015,0.020")
    )
    hedge_completion_latencies_ms: tuple[int, ...] = field(
        default_factory=lambda: _int_tuple("HEDGE_COMPLETION_LATENCIES_MS", "50,100,200")
    )
    hedge_size_candidates: tuple[Decimal, ...] = field(
        default_factory=lambda: _decimal_tuple("HEDGE_SIZE_CANDIDATES", "5,10,20,50")
    )
    hedge_latency_reserve_per_share: Decimal = field(
        default_factory=lambda: _decimal("HEDGE_LATENCY_RESERVE_PER_SHARE", "0.002")
    )
    hedge_min_expected_profit_usdc: Decimal = field(
        default_factory=lambda: _decimal("HEDGE_MIN_EXPECTED_PROFIT_USDC", "0.10")
    )
    hedge_max_improve_ticks: int = field(default_factory=lambda: _int("HEDGE_MAX_IMPROVE_TICKS", 1))
    hedge_max_quote_age_ms: int = field(default_factory=lambda: _int("HEDGE_MAX_QUOTE_AGE_MS", 30000))
    hedge_requote_cooldown_ms: int = field(default_factory=lambda: _int("HEDGE_REQUOTE_COOLDOWN_MS", 500))
    hedge_min_seconds_to_expiry: int = field(default_factory=lambda: _int("HEDGE_MIN_SECONDS_TO_EXPIRY", 30))
    hedge_extreme_probability: Decimal = field(default_factory=lambda: _decimal("HEDGE_EXTREME_PROBABILITY", "0.10"))
    hedge_taker_rebate_scenarios: tuple[Decimal, ...] = field(
        default_factory=lambda: _decimal_tuple("HEDGE_TAKER_REBATE_SCENARIOS", "0,0.03,0.08,0.18,0.30,0.50")
    )

    empirical_risk_min_samples: int = field(default_factory=lambda: _int("EMPIRICAL_RISK_MIN_SAMPLES", 20))
    use_empirical_risk_reserve: bool = field(default_factory=lambda: _bool("USE_EMPIRICAL_RISK_RESERVE", False))

    output_path: str = field(default_factory=lambda: _env("OUTPUT_PATH", "data/shadow_events.jsonl"))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO").upper())
    run_seconds: int = field(default_factory=lambda: _int("RUN_SECONDS", 0))

    gamma_url: str = "https://gamma-api.polymarket.com"
    websocket_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    geoblock_url: str = "https://polymarket.com/api/geoblock"
    crypto_taker_fee_rate: Decimal = Decimal("0.07")
