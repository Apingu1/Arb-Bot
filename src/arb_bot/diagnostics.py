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

    def maybe_log(
        self,
        engine: ArbitrageEngine,
        taker,
        edge_tracker: EdgeTracker,
        *,
        research=None,
        hedge=None,
        frontier=None,
        split_sell=None,
    ) -> None:
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
            "TAKER | eq=%+.4f pending=%d completed=%d misses=%d rejected=%d attempts=%d miss_rate=%.1f%% empirical_reserve=%s/share",
            float(taker.total_pnl),
            taker.pending_count,
            taker.completed,
            taker.leg_misses,
            taker.rejected,
            risk.attempts,
            float(risk.miss_probability * Decimal("100")),
            self._fmt(risk.estimated_reserve_per_share if risk.attempts else None),
        )

        if research is not None:
            for row in research.diagnostic_rows():
                if row["mode"] == "MAKER":
                    log.info(
                        "%s | eq=%+.4f pending=%d placed=%d completed=%d inventory_exits=%d partial=%d cancelled=%d | avg_queue=%.1f avg_first_fill=%.0fms avg_interleg=%.0fms | inv_rate=%.1f%% avg_inv_loss=%s/share reserve=%s/share surge_skips=%d risk_skips=%d max_dd=%.4f",
                        row["strategy"],
                        float(row["equity"]),
                        row["pending"],
                        row["placed"],
                        row["completed"],
                        row["inventory_exits"],
                        row["partial_exits"],
                        row["cancelled"],
                        float(row["avg_queue"]),
                        float(row["avg_first_fill_ms"]),
                        float(row["avg_interleg_ms"]),
                        float(row["risk_probability"] * Decimal("100")),
                        self._fmt(row["risk_loss_per_share"]),
                        self._fmt(row["risk_reserve"]),
                        row["surge_skips"],
                        row["risk_skips"],
                        float(row["max_drawdown"]),
                    )
                else:
                    log.info(
                        "%s | eq=%+.4f pending=%d placed=%d completed=%d inventory_exits=%d cancelled=%d | taker_complete=%d/%d misses=%d reprices=%d | avg_queue=%.1f inv_rate=%.1f%% avg_inv_loss=%s/share reserve=%s/share surge_skips=%d max_dd=%.4f",
                        row["strategy"],
                        float(row["equity"]),
                        row["pending"],
                        row["placed"],
                        row["completed"],
                        row["inventory_exits"],
                        row["cancelled"],
                        row["completion_successes"],
                        row["completion_attempts"],
                        row["completion_misses"],
                        row["reprices"],
                        float(row["avg_queue"]),
                        float(row["risk_probability"] * Decimal("100")),
                        self._fmt(row["risk_loss_per_share"]),
                        self._fmt(row["risk_reserve"]),
                        row["surge_skips"],
                        float(row["max_drawdown"]),
                    )

        if hedge is not None:
            for row in hedge.diagnostic_rows():
                log.info(
                    "%s | eq=%+.4f pending=%d placed=%d maker_fills=%d cancelled=%d | hedge=%d/%d misses=%d recover_complete=%d recover_unwind=%d | avg_queue=%.1f avg_fill=%.0fms avg_hedge=%.1fms | MID=%d/%+.4f EXTREME=%d/%+.4f | surge_skips=%d no_quote=%d max_dd=%.4f",
                    row["strategy"],
                    float(row["equity"]),
                    row["pending"],
                    row["placed"],
                    row["maker_fills"],
                    row["cancelled"],
                    row["hedge_successes"],
                    row["hedge_attempts"],
                    row["hedge_misses"],
                    row["recovery_completions"],
                    row["recovery_unwinds"],
                    float(row["avg_queue"]),
                    float(row["avg_fill_ms"]),
                    float(row["avg_hedge_latency_ms"]),
                    row["mid_events"],
                    float(row["mid_pnl"]),
                    row["extreme_events"],
                    float(row["extreme_pnl"]),
                    row["surge_skips"],
                    row["no_hedgeable_quote"],
                    float(row["max_drawdown"]),
                )

        if frontier is not None:
            for row in frontier.diagnostic_rows():
                reasons = row.get("cancel_reasons") or {}
                reason_text = ",".join(f"{k}:{v}" for k, v in sorted(reasons.items())) or "-"
                log.info(
                    "%s | eq=%+.4f placed=%d fill=%d p_fill=%.2f%% hedge=%d/%d p_h|f=%.2f%% | G=%dms S=%s qcons=%.1f%% | model_EV=%+.5f real_EV=%+.5f | ghost_fill=%d/%d ghost_target=%d | cancel={%s}",
                    row["strategy"],
                    float(row["equity"]),
                    row["placed"],
                    row["maker_fills"],
                    float(row["p_fill"] * Decimal("100")),
                    row["hedge_successes"],
                    row["hedge_attempts"],
                    float(row["p_hedge_given_fill"] * Decimal("100")),
                    row["grace_ms"],
                    self._fmt(row["fixed_size"]),
                    float(row["avg_cancel_queue_consumed"] * Decimal("100")),
                    float(row["modeled_ev_per_placement"]),
                    float(row["realized_ev_per_placement"]),
                    row["ghost_filled"],
                    row["ghost_created"],
                    row["ghost_target_profitable"],
                    reason_text,
                )
            ranked = frontier.ranked_rows()
            if ranked:
                top = ranked[:3]
                log.info(
                    "EV FRONTIER TOP | %s",
                    " | ".join(
                        f"{row['strategy']} model={float(row['modeled_ev_per_placement']):+.5f} real={float(row['realized_ev_per_placement']):+.5f}"
                        for row in top
                    ),
                )

        if split_sell is not None:
            for row in split_sell.diagnostic_rows():
                log.info(
                    "%s | eq=%+.4f pending=%d placed=%d first_fill=%d completed=%d residual=%d cancelled=%d | p_fill=%.2f%% p_complete|fill=%.2f%% avg_queue=%.1f avg_fill=%.0fms EV/place=%+.5f max_dd=%.4f",
                    row["strategy"],
                    float(row["equity"]),
                    row["pending"],
                    row["placed"],
                    row["first_fill_campaigns"],
                    row["completed"],
                    row["residual_exits"],
                    row["cancelled"],
                    float(row["p_fill"] * Decimal("100")),
                    float(row["p_complete_given_fill"] * Decimal("100")),
                    float(row["avg_queue"]),
                    float(row["avg_first_fill_ms"]),
                    float(row["ev_per_placement"]),
                    float(row["max_drawdown"]),
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
            surge_text = "-"
            if research is not None:
                surge = research.regime.current(pair.market_id)
                surge_text = (
                    f"ON reasons={','.join(surge.reasons) or 'pause'} move1={float(surge.move_1s):.3f} move3={float(surge.move_3s):.3f} ups={surge.updates_per_second}"
                    if surge.active
                    else f"off move1={float(surge.move_1s):.3f} move3={float(surge.move_3s):.3f} ups={surge.updates_per_second}"
                )

            if quote_a and quote_b:
                fees = taker_fee(quote_a.segments, engine.settings.crypto_taker_fee_rate) + taker_fee(
                    quote_b.segments, engine.settings.crypto_taker_fee_rate
                )
                risk_reserve = shares * engine.settings.risk_buffer_per_share
                net = shares - quote_a.notional - quote_b.notional - fees - risk_reserve
                net_edge = net / shares
                executable_pair = (quote_a.notional + quote_b.notional) / shares
                log.info(
                    "%s %s %s | %s %s/%s ask_depth=%s | %s %s/%s ask_depth=%s | taker_pair=%s raw=%+.4f %ssh_VWAP=%s fees=%.4f risk=%.4f net=%+.4f/share | maker_bids=%s maker_edge=%s | SURGE=%s | best_taker_pair=%s best_raw=%s best_net=%s obs=%d",
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
                    surge_text,
                    self._fmt(stats.best_pair_price if stats else None),
                    self._fmt_signed(stats.best_raw_edge if stats else None),
                    self._fmt_signed(stats.best_net_edge if stats else None),
                    stats.observations if stats else 0,
                )
            else:
                log.info(
                    "%s %s %s | %s %s/%s %s %s/%s | taker_pair=%s raw=%+.4f | insufficient depth for %ssh | maker_bids=%s maker_edge=%s | SURGE=%s | best_taker_pair=%s obs=%d",
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
                    surge_text,
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
