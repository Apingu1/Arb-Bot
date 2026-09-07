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
    output_path: str = field(default_factory=lambda: _env("OUTPUT_PATH", "data/shadow_events.jsonl"))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO").upper())
    run_seconds: int = field(default_factory=lambda: _int("RUN_SECONDS", 0))

    gamma_url: str = "https://gamma-api.polymarket.com"
    websocket_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    geoblock_url: str = "https://polymarket.com/api/geoblock"
    crypto_taker_fee_rate: Decimal = Decimal("0.07")
