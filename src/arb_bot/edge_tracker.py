from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from .discovery import MarketPhase, btc_15m_window_from_slug, market_phase
from .fees import taker_fee
from .storage import JsonlRecorder
from .strategy import ArbitrageEngine


ZERO = Decimal("0")
ONE = Decimal("1")


@dataclass(slots=True)
class WindowEdgeStats:
    observations: int = 0
    raw_positive: int = 0
    net_positive: int = 0
    qualifying: int = 0
    best_pair_price: Decimal | None = None
    best_raw_edge: Decimal | None = None
    best_net_edge: Decimal | None = None
    best_net_profit: Decimal | None = None
    last_pair_price: Decimal | None = None
    last_net_edge: Decimal | None = None


class EdgeTracker:
    """Record executable pair economics for every relevant market update.

    The tracker is deliberately independent of shadow submission thresholds.
    This lets us measure sub-$1 gaps that are too small to trade after fees and
    risk, as well as profitable-looking gaps that disappear before execution.
    """

    def __init__(self, recorder: JsonlRecorder, min_interval_ms: int = 0) -> None:
        self.recorder = recorder
        self.min_interval_seconds = max(0, min_interval_ms) / 1000
        self.stats: dict[str, WindowEdgeStats] = {}
        self._last_recorded: dict[str, float] = {}

    def observe(
        self,
        engine: ArbitrageEngine,
        market_id: str,
        *,
        source_event: str,
        exchange_timestamp: str | None = None,
        now_utc: datetime | None = None,
    ) -> dict[str, Any] | None:
        pair = engine.pairs.get(market_id)
        if not pair:
            return None

        now_utc = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
        phase = market_phase(pair, now_utc)
        if phase not in {MarketPhase.LIVE, MarketPhase.NEXT}:
            return None

        now_mono = time.monotonic()
        if self.min_interval_seconds:
            last = self._last_recorded.get(market_id, 0.0)
            if now_mono - last < self.min_interval_seconds:
                return None

        book_a = engine.books.get(pair.token_a)
        book_b = engine.books.get(pair.token_b)
        if not book_a or not book_b or not book_a.ready or not book_b.ready:
            return None

        ask_a = book_a.best_ask()
        ask_b = book_b.best_ask()
        bid_a = book_a.best_bid()
        bid_b = book_b.best_bid()
        if ask_a is None or ask_b is None:
            return None

        top_pair = ask_a + ask_b
        raw_edge = ONE - top_pair
        shares = engine.settings.min_trade_shares
        quote_a = book_a.quote_buy(shares)
        quote_b = book_b.quote_buy(shares)

        executable_pair: Decimal | None = None
        fees = ZERO
        net_profit: Decimal | None = None
        net_edge: Decimal | None = None
        qualifies = False

        if quote_a and quote_b:
            executable_pair = (quote_a.notional + quote_b.notional) / shares
            fees = taker_fee(quote_a.segments, engine.settings.crypto_taker_fee_rate) + taker_fee(
                quote_b.segments, engine.settings.crypto_taker_fee_rate
            )
            risk_reserve = shares * engine.settings.risk_buffer_per_share
            net_profit = shares - quote_a.notional - quote_b.notional - fees - risk_reserve
            net_edge = net_profit / shares
            qualifies = (
                phase == MarketPhase.LIVE
                and net_edge >= engine.settings.min_net_edge_per_share
                and net_profit >= engine.settings.min_expected_profit_usdc
            )

        window = btc_15m_window_from_slug(pair.slug)
        seconds_to_start = None
        seconds_to_end = None
        if window:
            start, end = window
            seconds_to_start = (start - now_utc).total_seconds()
            seconds_to_end = (end - now_utc).total_seconds()

        observation = {
            "observed_at": now_utc.isoformat().replace("+00:00", "Z"),
            "source_event": source_event,
            "exchange_timestamp": exchange_timestamp,
            "phase": phase.value,
            "market_id": pair.market_id,
            "slug": pair.slug,
            "question": pair.question,
            "outcome_a": pair.outcome_a,
            "outcome_b": pair.outcome_b,
            "best_bid_a": bid_a,
            "best_ask_a": ask_a,
            "best_bid_b": bid_b,
            "best_ask_b": ask_b,
            "best_ask_size_a": book_a.asks.get(ask_a, ZERO),
            "best_ask_size_b": book_b.asks.get(ask_b, ZERO),
            "top_pair_price": top_pair,
            "raw_edge_per_share": raw_edge,
            "min_trade_shares": shares,
            "executable_pair_price": executable_pair,
            "taker_fees": fees,
            "risk_reserve": shares * engine.settings.risk_buffer_per_share if quote_a and quote_b else None,
            "net_profit": net_profit,
            "net_edge_per_share": net_edge,
            "qualifies_shadow": qualifies,
            "book_age_ms_a": (now_mono - book_a.updated_monotonic) * 1000,
            "book_age_ms_b": (now_mono - book_b.updated_monotonic) * 1000,
            "seconds_to_start": seconds_to_start,
            "seconds_to_end": seconds_to_end,
        }

        self.recorder.write("edge_observation", observation)
        self._last_recorded[market_id] = now_mono
        self._update_stats(pair.slug, raw_edge, top_pair, net_edge, net_profit, qualifies)
        return observation

    def _update_stats(
        self,
        slug: str,
        raw_edge: Decimal,
        pair_price: Decimal,
        net_edge: Decimal | None,
        net_profit: Decimal | None,
        qualifies: bool,
    ) -> None:
        stats = self.stats.setdefault(slug, WindowEdgeStats())
        stats.observations += 1
        stats.last_pair_price = pair_price
        stats.last_net_edge = net_edge
        if raw_edge > ZERO:
            stats.raw_positive += 1
        if net_edge is not None and net_edge > ZERO:
            stats.net_positive += 1
        if qualifies:
            stats.qualifying += 1
        if stats.best_pair_price is None or pair_price < stats.best_pair_price:
            stats.best_pair_price = pair_price
        if stats.best_raw_edge is None or raw_edge > stats.best_raw_edge:
            stats.best_raw_edge = raw_edge
        if net_edge is not None and (stats.best_net_edge is None or net_edge > stats.best_net_edge):
            stats.best_net_edge = net_edge
        if net_profit is not None and (stats.best_net_profit is None or net_profit > stats.best_net_profit):
            stats.best_net_profit = net_profit

    def summary(self, slug: str) -> WindowEdgeStats | None:
        return self.stats.get(slug)
