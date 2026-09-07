from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from .config import Settings
from .discovery import MarketPhase, market_phase
from .fees import taker_fee
from .models import MarketPair
from .orderbook import TokenBook
from .storage import JsonlRecorder
from .strategy import ArbitrageEngine
from .strategy_metrics import StrategyEquity


log = logging.getLogger(__name__)
ZERO = Decimal("0")
ONE = Decimal("1")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(slots=True)
class MakerCampaign:
    market_id: str
    slug: str
    shares: Decimal
    bid_a: Decimal
    bid_b: Decimal
    placed_at: float
    placed_at_utc: str
    filled_a: bool = False
    filled_b: bool = False
    fill_price_a: Decimal | None = None
    fill_price_b: Decimal | None = None
    first_fill_at: float | None = None


class MakerShadowEngine:
    """Conservative passive complete-set simulator.

    Virtual orders rest at the observed best bids. A maker buy is only credited
    when a *post-placement* `last_trade_price` event reports a SELL at or below
    our bid with reported trade size at least as large as our simulated order.
    Queue position is still unknowable, so this remains a shadow approximation,
    but it is materially stronger than assuming that a transient crossed book
    or a best-bid update filled us.
    """

    strategy_name = "MAKER"

    def __init__(self, settings: Settings, recorder: JsonlRecorder) -> None:
        self.settings = settings
        self.recorder = recorder
        self.campaigns: dict[str, MakerCampaign] = {}
        self.equity = StrategyEquity(self.strategy_name, recorder)
        self.total_pnl = ZERO
        self.placed = 0
        self.completed = 0
        self.one_sided_unwinds = 0
        self.cancelled = 0
        self.last_summary: dict[str, Any] | None = None

    @property
    def pending_count(self) -> int:
        return len(self.campaigns)

    def on_market_update(self, engine: ArbitrageEngine, market_id: str) -> None:
        if not self.settings.maker_enabled:
            return
        pair = engine.pairs.get(market_id)
        if not pair or market_phase(pair) != MarketPhase.LIVE:
            return
        campaign = self.campaigns.get(market_id)
        if campaign is None:
            self._maybe_place(engine, pair)
            return
        self._check_fills(engine, pair, campaign)

    def process_due(self, engine: ArbitrageEngine) -> None:
        if not self.settings.maker_enabled:
            return
        now = time.monotonic()
        for market_id in list(self.campaigns):
            campaign = self.campaigns.get(market_id)
            if campaign is None:
                continue
            pair = engine.pairs.get(market_id)
            if pair is None:
                if not campaign.filled_a and not campaign.filled_b:
                    self.cancelled += 1
                    self.campaigns.pop(market_id, None)
                else:
                    filled_price = campaign.fill_price_a if campaign.filled_a else campaign.fill_price_b
                    assert filled_price is not None
                    self.one_sided_unwinds += 1
                    self._finalize(
                        campaign,
                        -(campaign.shares * filled_price),
                        status="ONE_SIDED_MAKER_FILL",
                        action="BOOK_PRUNED_WITH_OPEN_INVENTORY",
                        extra={"reason": "BOOK_PRUNED", "filled_leg": "A" if campaign.filled_a else "B"},
                    )
                continue

            self._check_fills(engine, pair, campaign)
            campaign = self.campaigns.get(market_id)
            if campaign is None:
                continue

            if campaign.first_fill_at is not None:
                age_ms = (now - campaign.first_fill_at) * 1000
                if age_ms >= self.settings.maker_inventory_timeout_ms:
                    self._unwind_one_sided(engine, pair, campaign, reason="INVENTORY_TIMEOUT")
                continue

            age_ms = (now - campaign.placed_at) * 1000
            if age_ms >= self.settings.maker_order_ttl_ms:
                self.cancelled += 1
                self.recorder.write(
                    "maker_campaign_cancelled",
                    {
                        "strategy": self.strategy_name,
                        "cancelled_at": _utc_now(),
                        "market_id": campaign.market_id,
                        "slug": campaign.slug,
                        "reason": "ORDER_TTL",
                        "shares": campaign.shares,
                        "bid_a": campaign.bid_a,
                        "bid_b": campaign.bid_b,
                    },
                )
                self.campaigns.pop(market_id, None)

    def _maybe_place(self, engine: ArbitrageEngine, pair: MarketPair) -> None:
        a = engine.books.get(pair.token_a)
        b = engine.books.get(pair.token_b)
        if not a or not b or not a.ready or not b.ready:
            return
        bid_a = a.best_bid()
        bid_b = b.best_bid()
        if bid_a is None or bid_b is None:
            return
        pair_cost = bid_a + bid_b
        gross_edge = ONE - pair_cost
        if gross_edge < self.settings.maker_min_gross_edge_per_share:
            return

        now = time.monotonic()
        campaign = MakerCampaign(
            market_id=pair.market_id,
            slug=pair.slug,
            shares=self.settings.maker_trade_shares,
            bid_a=bid_a,
            bid_b=bid_b,
            placed_at=now,
            placed_at_utc=_utc_now(),
        )
        self.campaigns[pair.market_id] = campaign
        self.placed += 1
        self.recorder.write(
            "maker_campaign_placed",
            {
                "strategy": self.strategy_name,
                "placed_at": campaign.placed_at_utc,
                "market_id": pair.market_id,
                "slug": pair.slug,
                "shares": campaign.shares,
                "maker_bid_a": bid_a,
                "maker_bid_b": bid_b,
                "combined_bid_cost": pair_cost,
                "gross_edge_per_share": gross_edge,
                "fill_model": "POST_PLACEMENT_SELL_TRADE_AT_OR_BELOW_BID_SIZE_GTE_ORDER",
                "order_ttl_ms": self.settings.maker_order_ttl_ms,
                "inventory_timeout_ms": self.settings.maker_inventory_timeout_ms,
            },
        )

    @staticmethod
    def _trade_fills(book: TokenBook, *, bid: Decimal, shares: Decimal, placed_at: float) -> bool:
        return (
            book.last_trade_monotonic >= placed_at
            and book.last_trade_side == "SELL"
            and book.last_trade_price is not None
            and book.last_trade_price <= bid
            and book.last_trade_size is not None
            and book.last_trade_size >= shares
        )

    def _check_fills(self, engine: ArbitrageEngine, pair: MarketPair, campaign: MakerCampaign) -> None:
        a = engine.books.get(pair.token_a)
        b = engine.books.get(pair.token_b)
        if not a or not b or not a.ready or not b.ready:
            return
        now = time.monotonic()

        if not campaign.filled_a and self._trade_fills(
            a, bid=campaign.bid_a, shares=campaign.shares, placed_at=campaign.placed_at
        ):
            campaign.filled_a = True
            campaign.fill_price_a = campaign.bid_a
            campaign.first_fill_at = campaign.first_fill_at or now
            self._record_fill(campaign, "A", campaign.bid_a, a)

        if not campaign.filled_b and self._trade_fills(
            b, bid=campaign.bid_b, shares=campaign.shares, placed_at=campaign.placed_at
        ):
            campaign.filled_b = True
            campaign.fill_price_b = campaign.bid_b
            campaign.first_fill_at = campaign.first_fill_at or now
            self._record_fill(campaign, "B", campaign.bid_b, b)

        if campaign.filled_a and campaign.filled_b:
            assert campaign.fill_price_a is not None and campaign.fill_price_b is not None
            pnl = campaign.shares * (ONE - campaign.fill_price_a - campaign.fill_price_b)
            self.completed += 1
            self._finalize(
                campaign,
                pnl,
                status="BOTH_MAKER_FILLED",
                action="MERGE_COMPLETE_SET",
                extra={"maker_fee": ZERO},
            )

    def _record_fill(self, campaign: MakerCampaign, leg: str, price: Decimal, book: TokenBook) -> None:
        self.recorder.write(
            "maker_leg_fill",
            {
                "strategy": self.strategy_name,
                "filled_at": _utc_now(),
                "market_id": campaign.market_id,
                "slug": campaign.slug,
                "leg": leg,
                "shares": campaign.shares,
                "maker_price": price,
                "confirming_trade_price": book.last_trade_price,
                "confirming_trade_size": book.last_trade_size,
                "confirming_trade_side": book.last_trade_side,
                "confirming_trade_timestamp": book.last_trade_timestamp,
                "maker_fee": ZERO,
            },
        )

    def _unwind_one_sided(
        self,
        engine: ArbitrageEngine,
        pair: MarketPair,
        campaign: MakerCampaign,
        *,
        reason: str,
    ) -> None:
        if campaign.filled_a == campaign.filled_b:
            return
        if campaign.filled_a:
            filled_price = campaign.fill_price_a
            filled_book = engine.books.get(pair.token_a)
            filled_leg = "A"
        else:
            filled_price = campaign.fill_price_b
            filled_book = engine.books.get(pair.token_b)
            filled_leg = "B"
        assert filled_price is not None

        unwind = filled_book.quote_sell(campaign.shares) if filled_book else None
        if unwind:
            unwind_fee = taker_fee(unwind.segments, self.settings.crypto_taker_fee_rate)
            pnl = unwind.notional - campaign.shares * filled_price - unwind_fee
            action = "UNWIND_ONE_SIDED_MAKER_INVENTORY"
        else:
            unwind_fee = ZERO
            pnl = -(campaign.shares * filled_price)
            action = "MAKER_UNWIND_LIQUIDITY_FAILURE"

        self.one_sided_unwinds += 1
        self._finalize(
            campaign,
            pnl,
            status="ONE_SIDED_MAKER_FILL",
            action=action,
            extra={
                "reason": reason,
                "filled_leg": filled_leg,
                "unwind_notional": unwind.notional if unwind else None,
                "unwind_average_price": unwind.average_price if unwind else None,
                "unwind_marginal_price": unwind.marginal_price if unwind else None,
                "unwind_taker_fee": unwind_fee,
            },
        )

    def _finalize(
        self,
        campaign: MakerCampaign,
        pnl: Decimal,
        *,
        status: str,
        action: str,
        extra: dict[str, Any],
    ) -> None:
        self.total_pnl += pnl
        equity_after = self.equity.apply(
            pnl,
            market_id=campaign.market_id,
            slug=campaign.slug,
            status=status,
            action=action,
        )
        summary = {
            "strategy": self.strategy_name,
            "finalized_at": _utc_now(),
            "market_id": campaign.market_id,
            "slug": campaign.slug,
            "status": status,
            "action": action,
            "shares": campaign.shares,
            "maker_bid_a": campaign.bid_a,
            "maker_bid_b": campaign.bid_b,
            "fill_price_a": campaign.fill_price_a,
            "fill_price_b": campaign.fill_price_b,
            "placed_at": campaign.placed_at_utc,
            "realized_pnl": pnl,
            "equity_after": equity_after,
            **extra,
        }
        self.last_summary = summary
        self.recorder.write("maker_execution_summary", summary)
        self.campaigns.pop(campaign.market_id, None)
        log.info(
            "MAKER %s %s | action=%s pnl=%+.4f pUSD | equity=%+.4f",
            status,
            campaign.slug,
            action,
            float(pnl),
            float(self.total_pnl),
        )
