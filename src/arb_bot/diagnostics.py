from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from .discovery import MarketPhase, btc_15m_window_from_slug, market_phase
from .edge_tracker import EdgeTracker
from .fees import taker_fee
from .strategy import ArbitrageEngine


log = logging.getLogger(__name__)
ZERO = Decimal("0")


class LiveDiagnostics:
    """Periodic visibility into feed health, market economics and shadow strategies."""

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

    def maybe_log(self, engine: ArbitrageEngine, taker, edge_tracker: EdgeTracker, maker=None, hybrid=None) -> None:
        now = time.monotonic()
        if now - self.last_log < self.interval_seconds:
            return

        elapsed = max(now - self.last_log, 0.001)
        new_messages = self.total_messages - self._messages_at_last_log
        rate = new_messages / elapsed
        ready_books = sum(1 for book in engine.books.values() if book.ready)
        now_utc = datetime.now(timezone.utc)

        live_count = sum(1 for pair in engine.pairs.values() if market_phase(pair, now_utc) == MarketPhase.LIVE)
        next_count = sum(1 for pair in engine.pairs.values() if market_phase(pair, now_utc) == MarketPhase.NEXT)

        log.info(
            "LIVE heartbeat | messages=%d (+%d, %.1f/s) book=%d price_change=%d touched=%d | books_ready=%d/%d | windows LIVE=%d NEXT=%d",
            self.total_messages,
            new_messages,
            rate,
            self.book_events,
            self.price_change_events,
            self.touched_events,
            ready_books,
            len(engine.books),
            live_count,
            next_count,
        )

        risk = taker.empirical_risk
        log.info(
            "STRATEGIES | TAKER eq=%+.4f pending=%d completed=%d misses=%d rejected=%d attempts=%d miss_rate=%.1f%% empirical_reserve=%s/share | MAKER eq=%+.4f pending=%d placed=%d completed=%d one_sided=%d cancelled=%d | HYBRID eq=%+.4f pending=%d placed=%d completed=%d maker_only=%d maker+taker=%d taker_misses=%d one_sided=%d",
            float(taker.total_pnl),
            taker.pending_count,
            taker.completed,
            taker.leg_misses,
            taker.rejected,
            risk.attempts,
            float(risk.miss_probability * Decimal("100")),
            self._fmt(risk.estimated_reserve_per_share if risk.attempts else None),
            float(maker.total_pnl) if maker else 0.0,
            maker.pending_count if maker else 0,
            maker.placed if maker else 0,
            maker.completed if maker else 0,
            maker.one_sided_unwinds if maker else 0,
            maker.cancelled if maker else 0,
            float(hybrid.total_pnl) if hybrid else 0.0,
            hybrid.pending_count if hybrid else 0,
            hybrid.placed if hybrid else 0,
            hybrid.completed if hybrid else 0,
            hybrid.completed_both_maker if hybrid else 0,
            hybrid.completed_with_taker if hybrid else 0,
            hybrid.completion_misses if hybrid else 0,
            hybrid.one_sided_unwinds if hybrid else 0,
        )

        ordered = sorted(
            engine.pairs.items(),
            key=lambda item: (0 if market_phase(item[1], now_utc) == MarketPhase.LIVE else 1, item[1].end_date or ""),
        )
        for _, pair in ordered:
            phase = market_phase(pair, now_utc)
            if phase not in {MarketPhase.LIVE, MarketPhase.NEXT}:
                continue

            a = engine.books.get(pair.token_a)
            b = engine.books.get(pair.token_b)
            if not a or not b:
                continue

            ask_a = a.best_ask()
            ask_b = b.best_ask()
            bid_a = a.best_bid()
            bid_b = b.best_bid()
            stats = edge_tracker.summary(pair.slug)
            window_text = self._window_text(pair.slug)

            if not a.ready or not b.ready or ask_a is None or ask_b is None:
                log.info(
                    "%s %s %s | awaiting books | %s=%s/%s %s=%s/%s",
                    phase.value,
                    pair.slug,
                    window_text,
                    pair.outcome_a,
                    self._fmt(bid_a),
                    self._fmt(ask_a),
                    pair.outcome_b,
                    self._fmt(bid_b),
                    self._fmt(ask_b),
                )
                continue

            raw_pair = ask_a + ask_b
            raw_edge = Decimal("1") - raw_pair
            maker_pair = bid_a + bid_b if bid_a is not None and bid_b is not None else None
            maker_edge = Decimal("1") - maker_pair if maker_pair is not None else None
            shares = engine.settings.min_trade_shares
            quote_a = a.quote_buy(shares)
            quote_b = b.quote_buy(shares)
            ask_depth_a = a.asks.get(ask_a, ZERO)
            ask_depth_b = b.asks.get(ask_b, ZERO)

            if quote_a and quote_b:
                fees = taker_fee(quote_a.segments, engine.settings.crypto_taker_fee_rate) + taker_fee(
                    quote_b.segments, engine.settings.crypto_taker_fee_rate
                )
                risk_reserve = shares * engine.settings.risk_buffer_per_share
                net = shares - quote_a.notional - quote_b.notional - fees - risk_reserve
                net_edge = net / shares
                executable_pair = (quote_a.notional + quote_b.notional) / shares
                log.info(
                    "%s %s %s | %s %s/%s ask_depth=%s | %s %s/%s ask_depth=%s | taker_pair=%s raw=%+.4f %ssh_VWAP=%s fees=%.4f risk=%.4f net=%+.4f/share | maker_bids=%s maker_edge=%s | best_taker_pair=%s best_raw=%s best_net=%s obs=%d",
                    phase.value,
                    pair.slug,
                    window_text,
                    pair.outcome_a,
                    self._fmt(bid_a),
                    self._fmt(ask_a),
                    self._fmt(ask_depth_a),
                    pair.outcome_b,
                    self._fmt(bid_b),
                    self._fmt(ask_b),
                    self._fmt(ask_depth_b),
                    self._fmt(raw_pair),
                    float(raw_edge),
                    self._fmt(shares),
                    self._fmt(executable_pair),
                    float(fees),
                    float(risk_reserve),
                    float(net_edge),
                    self._fmt(maker_pair),
                    self._fmt_signed(maker_edge),
                    self._fmt(stats.best_pair_price if stats else None),
                    self._fmt_signed(stats.best_raw_edge if stats else None),
                    self._fmt_signed(stats.best_net_edge if stats else None),
                    stats.observations if stats else 0,
                )
            else:
                log.info(
                    "%s %s %s | %s %s/%s %s %s/%s | taker_pair=%s raw=%+.4f | insufficient depth for %ssh | maker_bids=%s maker_edge=%s | best_taker_pair=%s obs=%d",
                    phase.value,
                    pair.slug,
                    window_text,
                    pair.outcome_a,
                    self._fmt(bid_a),
                    self._fmt(ask_a),
                    pair.outcome_b,
                    self._fmt(bid_b),
                    self._fmt(ask_b),
                    self._fmt(raw_pair),
                    float(raw_edge),
                    self._fmt(shares),
                    self._fmt(maker_pair),
                    self._fmt_signed(maker_edge),
                    self._fmt(stats.best_pair_price if stats else None),
                    stats.observations if stats else 0,
                )

        self.last_log = now
        self._messages_at_last_log = self.total_messages

    @staticmethod
    def _window_text(slug: str) -> str:
        window = btc_15m_window_from_slug(slug)
        if not window:
            return ""
        start, end = window
        return f"[{start:%H:%M}-{end:%H:%M}Z]"

    @staticmethod
    def _fmt(value: Decimal | None) -> str:
        if value is None:
            return "-"
        text = format(value.normalize(), "f")
        return text if text != "-0" else "0"

    @staticmethod
    def _fmt_signed(value: Decimal | None) -> str:
        if value is None:
            return "-"
        return f"{float(value):+.4f}"
