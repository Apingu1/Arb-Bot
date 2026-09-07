from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class MarketPair:
    market_id: str
    condition_id: str | None
    slug: str
    question: str
    outcome_a: str
    outcome_b: str
    token_a: str
    token_b: str
    end_date: str | None = None


@dataclass(frozen=True, slots=True)
class FillSegment:
    price: Decimal
    shares: Decimal


@dataclass(frozen=True, slots=True)
class ExecutionQuote:
    shares: Decimal
    notional: Decimal
    average_price: Decimal
    marginal_price: Decimal
    segments: tuple[FillSegment, ...]


@dataclass(frozen=True, slots=True)
class Opportunity:
    market_id: str
    slug: str
    question: str
    outcome_a: str
    outcome_b: str
    token_a: str
    token_b: str
    shares: Decimal
    detected_cost_a: Decimal
    detected_cost_b: Decimal
    detected_max_price_a: Decimal
    detected_max_price_b: Decimal
    gross_profit: Decimal
    taker_fees: Decimal
    risk_reserve: Decimal
    expected_net_profit: Decimal
    expected_net_edge_per_share: Decimal
    detected_monotonic: float
    detected_at_utc: str | None = None
    detected_best_ask_a: Decimal | None = None
    detected_best_ask_b: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ShadowResult:
    market_id: str
    slug: str
    status: str
    shares: Decimal
    pnl_usdc: Decimal
    action: str
    leg_a_filled: bool
    leg_b_filled: bool
    details: str
