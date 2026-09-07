from __future__ import annotations

import time
from datetime import datetime, timezone
from decimal import Decimal

from .config import Settings
from .fees import taker_fee
from .models import MarketPair, Opportunity
from .orderbook import TokenBook


class ArbitrageEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.books: dict[str, TokenBook] = {}
        self.pairs: dict[str, MarketPair] = {}
        self.token_to_pair: dict[str, str] = {}

    def set_markets(self, pairs: list[MarketPair]) -> None:
        self.pairs = {p.market_id: p for p in pairs}
        self.token_to_pair.clear()
        active_tokens: set[str] = set()
        for pair in pairs:
            active_tokens.update((pair.token_a, pair.token_b))
            self.token_to_pair[pair.token_a] = pair.market_id
            self.token_to_pair[pair.token_b] = pair.market_id
            self.books.setdefault(pair.token_a, TokenBook(pair.token_a))
            self.books.setdefault(pair.token_b, TokenBook(pair.token_b))
        self.books = {token: book for token, book in self.books.items() if token in active_tokens}

    def apply_event(self, event: dict) -> str | None:
        event_type = event.get("event_type")
        timestamp = event.get("timestamp")
        touched_market: str | None = None
        if event_type == "book":
            token = str(event.get("asset_id") or "")
            book = self.books.get(token)
            if book:
                book.apply_snapshot(event.get("bids") or [], event.get("asks") or [], timestamp)
                touched_market = self.token_to_pair.get(token)
        elif event_type == "price_change":
            for change in event.get("price_changes") or []:
                token = str(change.get("asset_id") or "")
                book = self.books.get(token)
                if not book:
                    continue
                book.apply_change(str(change.get("side") or ""), str(change.get("price") or "0"), str(change.get("size") or "0"), timestamp)
                touched_market = self.token_to_pair.get(token) or touched_market
        elif event_type == "last_trade_price":
            token = str(event.get("asset_id") or "")
            book = self.books.get(token)
            if book and event.get("price") is not None:
                book.apply_trade(
                    str(event.get("price")),
                    None if event.get("size") is None else str(event.get("size")),
                    str(event.get("side") or ""),
                    timestamp,
                )
                touched_market = self.token_to_pair.get(token)
        return touched_market

    def evaluate(self, market_id: str) -> Opportunity | None:
        pair = self.pairs.get(market_id)
        if not pair:
            return None
        a = self.books.get(pair.token_a)
        b = self.books.get(pair.token_b)
        if not a or not b or not a.ready or not b.ready:
            return None
        now = time.monotonic()
        max_age = self.settings.max_book_age_ms / 1000
        if now - a.updated_monotonic > max_age or now - b.updated_monotonic > max_age:
            return None

        candidates = a.ask_breakpoints(self.settings.max_trade_shares) | b.ask_breakpoints(self.settings.max_trade_shares)
        candidates.add(self.settings.min_trade_shares)
        candidates.add(self.settings.max_trade_shares)
        best: Opportunity | None = None
        detected_at_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        best_ask_a = a.best_ask()
        best_ask_b = b.best_ask()
        for shares in sorted(candidates):
            if shares < self.settings.min_trade_shares or shares > self.settings.max_trade_shares:
                continue
            quote_a = a.quote_buy(shares)
            quote_b = b.quote_buy(shares)
            if not quote_a or not quote_b:
                continue
            gross = shares - quote_a.notional - quote_b.notional
            fees = taker_fee(quote_a.segments, self.settings.crypto_taker_fee_rate) + taker_fee(quote_b.segments, self.settings.crypto_taker_fee_rate)
            risk_reserve = shares * self.settings.risk_buffer_per_share
            expected = gross - fees - risk_reserve
            edge = expected / shares
            if edge < self.settings.min_net_edge_per_share or expected < self.settings.min_expected_profit_usdc:
                continue
            opportunity = Opportunity(
                market_id=pair.market_id,
                slug=pair.slug,
                question=pair.question,
                outcome_a=pair.outcome_a,
                outcome_b=pair.outcome_b,
                token_a=pair.token_a,
                token_b=pair.token_b,
                shares=shares,
                detected_cost_a=quote_a.notional,
                detected_cost_b=quote_b.notional,
                detected_max_price_a=quote_a.marginal_price,
                detected_max_price_b=quote_b.marginal_price,
                gross_profit=gross,
                taker_fees=fees,
                risk_reserve=risk_reserve,
                expected_net_profit=expected,
                expected_net_edge_per_share=edge,
                detected_monotonic=now,
                detected_at_utc=detected_at_utc,
                detected_best_ask_a=best_ask_a,
                detected_best_ask_b=best_ask_b,
            )
            if best is None or opportunity.expected_net_profit > best.expected_net_profit:
                best = opportunity
        return best
