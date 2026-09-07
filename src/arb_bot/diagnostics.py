from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Any

from .fees import taker_fee
from .strategy import ArbitrageEngine


log = logging.getLogger(__name__)
ZERO = Decimal("0")


class LiveDiagnostics:
    """Periodic visibility into the shadow bot's live market-data path.

    This deliberately does not influence trading decisions.  It exists so a
    quiet terminal can be distinguished from a dead feed: we report message
    throughput, book readiness and the currently observable pair economics.
    """

    def __init__(self, interval_seconds: int) -> None:
        self.interval_seconds = max(1, interval_seconds)
        self.started = time.monotonic()
        self.last_log = self.started
        self.total_messages = 0
        self.book_events = 0
        self.price_change_events = 0
        self.touched_events = 0
        self._messages_at_last_log = 0

    def observe(self, message: dict[str, Any], market_id: str | None) -> None:
        self.total_messages += 1
        event_type = str(message.get("event_type") or "")
        if event_type == "book":
            self.book_events += 1
        elif event_type == "price_change":
            self.price_change_events += 1
        if market_id:
            self.touched_events += 1

    def due(self) -> bool:
        return time.monotonic() - self.last_log >= self.interval_seconds

    def maybe_log(self, engine: ArbitrageEngine, shadow) -> None:
        now = time.monotonic()
        if now - self.last_log < self.interval_seconds:
            return

        elapsed = max(now - self.last_log, 0.001)
        new_messages = self.total_messages - self._messages_at_last_log
        rate = new_messages / elapsed
        ready_books = sum(1 for book in engine.books.values() if book.ready)

        log.info(
            "LIVE heartbeat | messages=%d (+%d, %.1f/s) book=%d price_change=%d touched=%d | books_ready=%d/%d | shadow pending=%d completed=%d leg_misses=%d rejected=%d pnl=%+.4f pUSD",
            self.total_messages,
            new_messages,
            rate,
            self.book_events,
            self.price_change_events,
            self.touched_events,
            ready_books,
            len(engine.books),
            len(shadow.pending),
            shadow.completed,
            shadow.leg_misses,
            shadow.rejected,
            float(shadow.total_pnl),
        )

        for market_id, pair in sorted(engine.pairs.items(), key=lambda item: item[1].end_date or ""):
            a = engine.books.get(pair.token_a)
            b = engine.books.get(pair.token_b)
            if not a or not b:
                continue

            ask_a = a.best_ask()
            ask_b = b.best_ask()
            if not a.ready or not b.ready or ask_a is None or ask_b is None:
                log.info(
                    "MARKET %s | awaiting books | %s=%s %s=%s",
                    pair.slug,
                    pair.outcome_a,
                    self._fmt(ask_a),
                    pair.outcome_b,
                    self._fmt(ask_b),
                )
                continue

            raw_pair = ask_a + ask_b
            raw_edge = Decimal("1") - raw_pair
            shares = engine.settings.min_trade_shares
            quote_a = a.quote_buy(shares)
            quote_b = b.quote_buy(shares)

            if quote_a and quote_b:
                fees = taker_fee(quote_a.segments, engine.settings.crypto_taker_fee_rate) + taker_fee(
                    quote_b.segments, engine.settings.crypto_taker_fee_rate
                )
                risk = shares * engine.settings.risk_buffer_per_share
                net = shares - quote_a.notional - quote_b.notional - fees - risk
                net_edge = net / shares
                executable_pair = (quote_a.notional + quote_b.notional) / shares
                log.info(
                    "MARKET %s | %s ask=%s %s ask=%s | top_pair=%s raw_edge=%+.4f | min_size=%s exec_pair=%s net_edge=%+.4f/share",
                    pair.slug,
                    pair.outcome_a,
                    self._fmt(ask_a),
                    pair.outcome_b,
                    self._fmt(ask_b),
                    self._fmt(raw_pair),
                    float(raw_edge),
                    self._fmt(shares),
                    self._fmt(executable_pair),
                    float(net_edge),
                )
            else:
                log.info(
                    "MARKET %s | %s ask=%s %s ask=%s | top_pair=%s raw_edge=%+.4f | insufficient depth for min_size=%s",
                    pair.slug,
                    pair.outcome_a,
                    self._fmt(ask_a),
                    pair.outcome_b,
                    self._fmt(ask_b),
                    self._fmt(raw_pair),
                    float(raw_edge),
                    self._fmt(shares),
                )

        self.last_log = now
        self._messages_at_last_log = self.total_messages

    @staticmethod
    def _fmt(value: Decimal | None) -> str:
        if value is None:
            return "-"
        text = format(value.normalize(), "f")
        return text if text != "-0" else "0"
