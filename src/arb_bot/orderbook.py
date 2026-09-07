from __future__ import annotations

import time
from decimal import Decimal

from .models import ExecutionQuote, FillSegment


ZERO = Decimal("0")


class TokenBook:
    def __init__(self, token_id: str) -> None:
        self.token_id = token_id
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}
        self.updated_monotonic: float = 0.0
        self.exchange_timestamp: str | None = None
        self.ready = False
        self.last_trade_price: Decimal | None = None
        self.last_trade_size: Decimal | None = None
        self.last_trade_side: str | None = None
        self.last_trade_monotonic: float = 0.0
        self.last_trade_timestamp: str | None = None

    def apply_snapshot(self, bids: list[dict], asks: list[dict], timestamp: str | None = None) -> None:
        self.bids = self._levels_to_dict(bids)
        self.asks = self._levels_to_dict(asks)
        self.exchange_timestamp = timestamp
        self.updated_monotonic = time.monotonic()
        self.ready = True

    def apply_change(self, side: str, price: str, size: str, timestamp: str | None = None) -> None:
        px = Decimal(str(price))
        qty = Decimal(str(size))
        levels = self.bids if side.upper() == "BUY" else self.asks
        if qty <= ZERO:
            levels.pop(px, None)
        else:
            levels[px] = qty
        self.exchange_timestamp = timestamp
        self.updated_monotonic = time.monotonic()

    def apply_trade(self, price: str, size: str | None, side: str, timestamp: str | None = None) -> None:
        self.last_trade_price = Decimal(str(price))
        self.last_trade_size = Decimal(str(size)) if size not in {None, ""} else None
        self.last_trade_side = str(side or "").upper()
        self.last_trade_timestamp = timestamp
        self.last_trade_monotonic = time.monotonic()

    @staticmethod
    def _levels_to_dict(levels: list[dict]) -> dict[Decimal, Decimal]:
        result: dict[Decimal, Decimal] = {}
        for level in levels or []:
            px = Decimal(str(level["price"]))
            qty = Decimal(str(level["size"]))
            if qty > ZERO:
                result[px] = qty
        return result

    def best_ask(self) -> Decimal | None:
        return min(self.asks) if self.asks else None

    def best_bid(self) -> Decimal | None:
        return max(self.bids) if self.bids else None

    def quote_buy(self, shares: Decimal, max_price: Decimal | None = None) -> ExecutionQuote | None:
        return self._quote(self.asks, shares, ascending=True, limit=max_price)

    def quote_sell(self, shares: Decimal, min_price: Decimal | None = None) -> ExecutionQuote | None:
        return self._quote(self.bids, shares, ascending=False, limit=min_price)

    @staticmethod
    def _quote(levels: dict[Decimal, Decimal], shares: Decimal, *, ascending: bool, limit: Decimal | None) -> ExecutionQuote | None:
        if shares <= ZERO:
            return None
        remaining = shares
        segments: list[FillSegment] = []
        notional = ZERO
        marginal = ZERO
        for px in sorted(levels, reverse=not ascending):
            if limit is not None:
                if ascending and px > limit:
                    break
                if not ascending and px < limit:
                    break
            available = levels[px]
            take = min(remaining, available)
            if take <= ZERO:
                continue
            segments.append(FillSegment(px, take))
            notional += px * take
            marginal = px
            remaining -= take
            if remaining <= ZERO:
                return ExecutionQuote(shares, notional, notional / shares, marginal, tuple(segments))
        return None

    def ask_breakpoints(self, max_shares: Decimal) -> set[Decimal]:
        total = ZERO
        points: set[Decimal] = set()
        for px in sorted(self.asks):
            total += self.asks[px]
            points.add(min(total, max_shares))
            if total >= max_shares:
                break
        return points
