from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR
from typing import Any

from .config import Settings
from .discovery import MarketPhase, btc_15m_window_from_slug, market_phase
from .fees import taker_fee
from .maker_research import MarketRegimeTracker, QueueLeg, SurgeSnapshot
from .models import ExecutionQuote, MarketPair
from .orderbook import TokenBook
from .storage import JsonlRecorder
from .strategy import ArbitrageEngine
from .strategy_metrics import StrategyEquity


log = logging.getLogger(__name__)
ZERO = Decimal("0")
ONE = Decimal("1")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _floor_to_tick(value: Decimal, tick: Decimal) -> Decimal:
    if value <= ZERO or tick <= ZERO:
        return ZERO
    return (value / tick).to_integral_value(rounding=ROUND_FLOOR) * tick


def _quote_payload(quote: ExecutionQuote | None) -> dict[str, Any] | None:
    if quote is None:
        return None
    return {
        "shares": quote.shares,
        "notional": quote.notional,
        "average_price": quote.average_price,
        "marginal_price": quote.marginal_price,
        "segments": [{"price": item.price, "shares": item.shares} for item in quote.segments],
    }


def _edge_label(edge: Decimal) -> str:
    # Tenths of a cent: 0.005 -> 05, 0.010 -> 10, 0.015 -> 15.
    units = int((edge * Decimal("1000")).to_integral_value())
    return f"{units:02d}"


@dataclass(slots=True)
class HedgeIntent:
    maker_side: str
    hedge_side: str
    shares: Decimal
    maker_price: Decimal
    opposite_quote: ExecutionQuote
    opposite_fee: Decimal
    expected_profit_after_reserve: Decimal
    expected_edge_after_reserve: Decimal
    queue_ahead: Decimal
    regime: str


@dataclass(slots=True)
class PendingHedge:
    shares: Decimal
    detected_at: float
    detected_at_utc: str
    execute_at: float
    max_price: Decimal
    detected_quote: ExecutionQuote
    expected_profit_after_reserve: Decimal
    expected_edge_after_reserve: Decimal


@dataclass(slots=True)
class PendingRecovery:
    execute_at: float
    reason: str


@dataclass(slots=True)
class HedgeCampaign:
    market_id: str
    slug: str
    maker_side: str
    hedge_side: str
    target_shares: Decimal
    maker_price: Decimal
    placed_at: float
    placed_at_utc: str
    queue: QueueLeg
    regime: str
    placement_hedge_quote: ExecutionQuote
    placement_hedge_fee: Decimal
    placement_expected_profit: Decimal
    placement_expected_edge: Decimal
    maker_fill_qty: Decimal = ZERO
    maker_fill_notional: Decimal = ZERO
    maker_fill_at: float | None = None
    maker_fill_at_utc: str | None = None
    cancelled_remainder: Decimal = ZERO
    pending_hedge: PendingHedge | None = None
    pending_recovery: PendingRecovery | None = None
    final_taker_fee: Decimal = ZERO

    @property
    def has_fill(self) -> bool:
        return self.maker_fill_qty > ZERO


class HedgeableVariantEngine:
    """Queue-aware maker-first strategy with a pre-existing taker hedge.

    A maker order is only placed if the *current opposite executable ask book*
    already allows a complete set with the configured target net edge after
    taker fee and latency reserve. Once any maker quantity fills, the unfilled
    maker remainder is cancelled and only that filled quantity is hedged.
    """

    def __init__(
        self,
        settings: Settings,
        recorder: JsonlRecorder,
        regime_tracker: MarketRegimeTracker,
        *,
        edge_target: Decimal,
        completion_latency_ms: int,
    ) -> None:
        self.settings = settings
        self.recorder = recorder
        self.regime_tracker = regime_tracker
        self.edge_target = edge_target
        self.completion_latency_ms = completion_latency_ms
        self.strategy_name = f"HEDGE-{_edge_label(edge_target)}-L{completion_latency_ms}"
        self.campaigns: dict[str, HedgeCampaign] = {}
        self.cooldown_until: dict[str, float] = {}
        self.equity = StrategyEquity(self.strategy_name, recorder)
        self.total_pnl = ZERO

        self.placed = 0
        self.cancelled = 0
        self.maker_fills = 0
        self.hedge_attempts = 0
        self.hedge_successes = 0
        self.hedge_misses = 0
        self.recovery_completions = 0
        self.recovery_unwinds = 0
        self.recovery_failures = 0
        self.surge_skips = 0
        self.no_hedgeable_quote = 0
        self.total_initial_queue = ZERO
        self.total_time_to_fill_ms = Decimal("0")
        self.total_actual_hedge_latency_ms = Decimal("0")
        self.mid_events = 0
        self.mid_pnl = ZERO
        self.extreme_events = 0
        self.extreme_pnl = ZERO
        self.last_summary: dict[str, Any] | None = None

    @property
    def pending_count(self) -> int:
        return len(self.campaigns)

    @property
    def average_initial_queue(self) -> Decimal:
        if self.placed <= 0:
            return ZERO
        return self.total_initial_queue / Decimal(self.placed)

    @property
    def average_time_to_fill_ms(self) -> Decimal:
        if self.maker_fills <= 0:
            return ZERO
        return self.total_time_to_fill_ms / Decimal(self.maker_fills)

    @property
    def average_hedge_latency_ms(self) -> Decimal:
        attempts = self.hedge_successes + self.hedge_misses
        if attempts <= 0:
            return ZERO
        return self.total_actual_hedge_latency_ms / Decimal(attempts)

    def on_market_update(self, engine: ArbitrageEngine, market_id: str, surge: SurgeSnapshot) -> None:
        if not self.settings.hedge_enabled:
            return
        pair = engine.pairs.get(market_id)
        if not pair or market_phase(pair) != MarketPhase.LIVE:
            return

        campaign = self.campaigns.get(market_id)
        if campaign is None:
            self._maybe_place(engine, pair, surge)
            return

        if not campaign.has_fill:
            fill = self._consume_maker_trade(engine, pair, campaign)
            if fill > ZERO:
                self._on_maker_fill(engine, pair, campaign, fill)
                return

            if surge.active:
                self._cancel(campaign, "SURGE")
                return
            if not self._still_hedgeable(engine, pair, campaign):
                self._cancel(campaign, "HEDGEABILITY_DROPPED")

    def process_due(self, engine: ArbitrageEngine) -> None:
        now = time.monotonic()
        for market_id in list(self.campaigns):
            campaign = self.campaigns.get(market_id)
            if campaign is None:
                continue
            pair = engine.pairs.get(market_id)

            if pair is None or market_phase(pair) != MarketPhase.LIVE:
                if campaign.has_fill:
                    self._recover_now(engine, pair, campaign, reason="WINDOW_ROLLOVER")
                else:
                    self._cancel(campaign, "WINDOW_ROLLOVER")
                continue

            if campaign.pending_hedge and campaign.pending_hedge.execute_at <= now:
                self._execute_hedge(engine, pair, campaign)
                campaign = self.campaigns.get(market_id)
                if campaign is None:
                    continue

            if campaign.pending_recovery and campaign.pending_recovery.execute_at <= now:
                self._execute_recovery(engine, pair, campaign)
                continue

            if campaign.has_fill:
                continue

            if self.regime_tracker.current(market_id).active:
                self._cancel(campaign, "SURGE")
                continue
            if (now - campaign.placed_at) * 1000 >= self.settings.hedge_max_quote_age_ms:
                self._cancel(campaign, "MAX_QUOTE_AGE")
                continue
            if self._seconds_to_expiry(pair.slug) <= self.settings.hedge_min_seconds_to_expiry:
                self._cancel(campaign, "NEAR_EXPIRY")
                continue
            if not self._still_hedgeable(engine, pair, campaign):
                self._cancel(campaign, "HEDGEABILITY_DROPPED")

    def _seconds_to_expiry(self, slug: str) -> float:
        window = btc_15m_window_from_slug(slug)
        if not window:
            return 999999.0
        _, end = window
        return max(0.0, (end - datetime.now(timezone.utc)).total_seconds())

    def _market_regime(self, engine: ArbitrageEngine, pair: MarketPair) -> str:
        a = engine.books.get(pair.token_a)
        if not a or a.best_bid() is None or a.best_ask() is None:
            return "UNKNOWN"
        midpoint = (a.best_bid() + a.best_ask()) / Decimal("2")
        threshold = self.settings.hedge_extreme_probability
        return "EXTREME" if midpoint <= threshold or midpoint >= ONE - threshold else "MID"

    def _side_intent(
        self,
        engine: ArbitrageEngine,
        pair: MarketPair,
        *,
        maker_side: str,
        shares: Decimal,
    ) -> HedgeIntent | None:
        maker_book = engine.books.get(pair.token_a if maker_side == "A" else pair.token_b)
        hedge_book = engine.books.get(pair.token_b if maker_side == "A" else pair.token_a)
        if not maker_book or not hedge_book or not maker_book.ready or not hedge_book.ready:
            return None
        best_bid = maker_book.best_bid()
        best_ask = maker_book.best_ask()
        if best_bid is None or best_ask is None:
            return None

        quote = hedge_book.quote_buy(shares)
        if quote is None:
            return None
        fee = taker_fee(quote.segments, self.settings.crypto_taker_fee_rate)
        reserve = shares * self.settings.hedge_latency_reserve_per_share
        required_profit = shares * self.edge_target
        max_maker_notional = shares - quote.notional - fee - reserve - required_profit
        if max_maker_notional <= ZERO:
            return None
        max_maker_price = max_maker_notional / shares

        tick = self.settings.maker_tick_size
        passive_cap = best_ask - tick
        improve_cap = best_bid + tick * Decimal(self.settings.hedge_max_improve_ticks)
        maker_price = min(_floor_to_tick(max_maker_price, tick), passive_cap, improve_cap)
        if maker_price < tick:
            return None

        expected = shares - shares * maker_price - quote.notional - fee - reserve
        edge = expected / shares
        if edge < self.edge_target or expected < self.settings.hedge_min_expected_profit_usdc:
            return None

        queue = maker_book.bids.get(maker_price, ZERO)
        return HedgeIntent(
            maker_side=maker_side,
            hedge_side="B" if maker_side == "A" else "A",
            shares=shares,
            maker_price=maker_price,
            opposite_quote=quote,
            opposite_fee=fee,
            expected_profit_after_reserve=expected,
            expected_edge_after_reserve=edge,
            queue_ahead=queue,
            regime=self._market_regime(engine, pair),
        )

    def _best_intent(self, engine: ArbitrageEngine, pair: MarketPair) -> HedgeIntent | None:
        best: HedgeIntent | None = None
        for maker_side in ("A", "B"):
            side_best: HedgeIntent | None = None
            for shares in sorted(self.settings.hedge_size_candidates):
                if shares <= ZERO or shares > self.settings.max_trade_shares:
                    continue
                intent = self._side_intent(engine, pair, maker_side=maker_side, shares=shares)
                if intent is not None:
                    # Deliberately choose the largest fully hedgeable configured
                    # size for each direction, as requested by the research plan.
                    side_best = intent
            if side_best is None:
                continue
            if best is None:
                best = side_best
                continue
            rank = (side_best.shares, side_best.expected_profit_after_reserve, -side_best.queue_ahead)
            best_rank = (best.shares, best.expected_profit_after_reserve, -best.queue_ahead)
            if rank > best_rank:
                best = side_best
        return best

    def _maybe_place(self, engine: ArbitrageEngine, pair: MarketPair, surge: SurgeSnapshot) -> None:
        now = time.monotonic()
        if now < self.cooldown_until.get(pair.market_id, 0.0):
            return
        if surge.active:
            self.surge_skips += 1
            return
        if self._seconds_to_expiry(pair.slug) <= self.settings.hedge_min_seconds_to_expiry:
            return

        intent = self._best_intent(engine, pair)
        if intent is None:
            self.no_hedgeable_quote += 1
            return
        maker_book = engine.books.get(pair.token_a if intent.maker_side == "A" else pair.token_b)
        assert maker_book is not None
        campaign = HedgeCampaign(
            market_id=pair.market_id,
            slug=pair.slug,
            maker_side=intent.maker_side,
            hedge_side=intent.hedge_side,
            target_shares=intent.shares,
            maker_price=intent.maker_price,
            placed_at=now,
            placed_at_utc=_utc_now(),
            queue=QueueLeg(
                price=intent.maker_price,
                initial_queue_ahead=intent.queue_ahead,
                queue_ahead=intent.queue_ahead,
                last_trade_seen=maker_book.last_trade_monotonic,
                last_reprice_at=now,
            ),
            regime=intent.regime,
            placement_hedge_quote=intent.opposite_quote,
            placement_hedge_fee=intent.opposite_fee,
            placement_expected_profit=intent.expected_profit_after_reserve,
            placement_expected_edge=intent.expected_edge_after_reserve,
        )
        self.campaigns[pair.market_id] = campaign
        self.placed += 1
        self.total_initial_queue += intent.queue_ahead
        self.recorder.write(
            "hedge_campaign_placed",
            {
                "strategy": self.strategy_name,
                "placed_at": campaign.placed_at_utc,
                "market_id": pair.market_id,
                "slug": pair.slug,
                "regime": campaign.regime,
                "maker_side": campaign.maker_side,
                "hedge_side": campaign.hedge_side,
                "shares": campaign.target_shares,
                "maker_price": campaign.maker_price,
                "initial_queue_ahead": campaign.queue.initial_queue_ahead,
                "opposite_taker_quote": _quote_payload(campaign.placement_hedge_quote),
                "opposite_taker_fee": campaign.placement_hedge_fee,
                "target_net_edge_per_share": self.edge_target,
                "latency_reserve_per_share": self.settings.hedge_latency_reserve_per_share,
                "expected_profit_after_reserve": campaign.placement_expected_profit,
                "expected_edge_after_reserve": campaign.placement_expected_edge,
                "completion_latency_ms": self.completion_latency_ms,
            },
        )

    def _consume_maker_trade(self, engine: ArbitrageEngine, pair: MarketPair, campaign: HedgeCampaign) -> Decimal:
        book = engine.books.get(pair.token_a if campaign.maker_side == "A" else pair.token_b)
        if not book or book.last_trade_monotonic <= campaign.queue.last_trade_seen:
            return ZERO
        campaign.queue.last_trade_seen = book.last_trade_monotonic
        if (
            book.last_trade_side != "SELL"
            or book.last_trade_price is None
            or book.last_trade_size is None
            or book.last_trade_size <= ZERO
            or book.last_trade_price > campaign.maker_price
        ):
            return ZERO

        if book.last_trade_price < campaign.maker_price:
            fill = campaign.target_shares
            campaign.queue.queue_ahead = ZERO
        else:
            traded = book.last_trade_size
            queue_consumed = min(campaign.queue.queue_ahead, traded)
            campaign.queue.queue_ahead -= queue_consumed
            overflow = max(traded - queue_consumed, ZERO)
            fill = min(campaign.target_shares, overflow)

        if fill > ZERO:
            campaign.queue.filled_qty += fill
            campaign.queue.filled_notional += fill * campaign.maker_price
        return fill

    def _decision_economics(
        self,
        quote: ExecutionQuote,
        maker_price: Decimal,
        shares: Decimal,
    ) -> tuple[Decimal, Decimal, Decimal]:
        fee = taker_fee(quote.segments, self.settings.crypto_taker_fee_rate)
        reserve = shares * self.settings.hedge_latency_reserve_per_share
        net = shares - shares * maker_price - quote.notional - fee - reserve
        return net, net / shares, fee

    def _on_maker_fill(self, engine: ArbitrageEngine, pair: MarketPair, campaign: HedgeCampaign, fill: Decimal) -> None:
        now = time.monotonic()
        campaign.maker_fill_qty = fill
        campaign.maker_fill_notional = fill * campaign.maker_price
        campaign.maker_fill_at = now
        campaign.maker_fill_at_utc = _utc_now()
        campaign.cancelled_remainder = max(campaign.target_shares - fill, ZERO)
        self.maker_fills += 1
        self.total_time_to_fill_ms += Decimal(str((now - campaign.placed_at) * 1000))
        self.recorder.write(
            "hedge_maker_fill",
            {
                "strategy": self.strategy_name,
                "filled_at": campaign.maker_fill_at_utc,
                "market_id": campaign.market_id,
                "slug": campaign.slug,
                "regime": campaign.regime,
                "maker_side": campaign.maker_side,
                "maker_price": campaign.maker_price,
                "fill_qty": fill,
                "cancelled_unfilled_remainder": campaign.cancelled_remainder,
                "queue_ahead_remaining": campaign.queue.queue_ahead,
            },
        )

        hedge_book = engine.books.get(pair.token_b if campaign.hedge_side == "B" else pair.token_a)
        quote = hedge_book.quote_buy(fill) if hedge_book else None
        if quote is not None:
            net, edge, _ = self._decision_economics(quote, campaign.maker_price, fill)
            if edge >= self.edge_target:
                campaign.pending_hedge = PendingHedge(
                    shares=fill,
                    detected_at=now,
                    detected_at_utc=_utc_now(),
                    execute_at=now + self.completion_latency_ms / 1000,
                    max_price=quote.marginal_price,
                    detected_quote=quote,
                    expected_profit_after_reserve=net,
                    expected_edge_after_reserve=edge,
                )
                self.hedge_attempts += 1
                self.recorder.write(
                    "hedge_completion_intent",
                    {
                        "strategy": self.strategy_name,
                        "market_id": campaign.market_id,
                        "slug": campaign.slug,
                        "maker_side": campaign.maker_side,
                        "hedge_side": campaign.hedge_side,
                        "shares": fill,
                        "maker_price": campaign.maker_price,
                        "taker_quote": _quote_payload(quote),
                        "expected_profit_after_reserve": net,
                        "expected_edge_after_reserve": edge,
                        "completion_latency_ms": self.completion_latency_ms,
                    },
                )
                return

        # The pre-existing hedge vanished or no longer clears the original edge.
        # Do not hold directional inventory hoping it comes back: enter recovery.
        campaign.pending_recovery = PendingRecovery(
            execute_at=now + self.settings.shadow_recovery_latency_ms / 1000,
            reason="HEDGE_NOT_PROFITABLE_AT_MAKER_FILL",
        )

    def _still_hedgeable(self, engine: ArbitrageEngine, pair: MarketPair, campaign: HedgeCampaign) -> bool:
        hedge_book = engine.books.get(pair.token_b if campaign.hedge_side == "B" else pair.token_a)
        if not hedge_book or not hedge_book.ready:
            return False
        quote = hedge_book.quote_buy(campaign.target_shares)
        if quote is None:
            return False
        net, edge, _ = self._decision_economics(quote, campaign.maker_price, campaign.target_shares)
        return edge >= self.edge_target and net >= self.settings.hedge_min_expected_profit_usdc

    def _execute_hedge(self, engine: ArbitrageEngine, pair: MarketPair, campaign: HedgeCampaign) -> None:
        pending = campaign.pending_hedge
        if pending is None:
            return
        hedge_book = engine.books.get(pair.token_b if campaign.hedge_side == "B" else pair.token_a)
        actual_latency = Decimal(str((time.monotonic() - pending.detected_at) * 1000))
        fill = hedge_book.quote_buy(pending.shares, max_price=pending.max_price) if hedge_book else None
        self.total_actual_hedge_latency_ms += actual_latency
        campaign.pending_hedge = None

        if fill is None:
            self.hedge_misses += 1
            self.recorder.write(
                "hedge_completion_miss",
                {
                    "strategy": self.strategy_name,
                    "market_id": campaign.market_id,
                    "slug": campaign.slug,
                    "shares": pending.shares,
                    "detected_quote": _quote_payload(pending.detected_quote),
                    "max_price": pending.max_price,
                    "actual_latency_ms": actual_latency,
                },
            )
            campaign.pending_recovery = PendingRecovery(
                execute_at=time.monotonic() + self.settings.shadow_recovery_latency_ms / 1000,
                reason="HEDGE_FOK_MISS",
            )
            return

        fee = taker_fee(fill.segments, self.settings.crypto_taker_fee_rate)
        pnl = pending.shares - campaign.maker_fill_notional - fill.notional - fee
        campaign.final_taker_fee = fee
        self.hedge_successes += 1
        self._finalize(
            campaign,
            pnl,
            status="HEDGE_COMPLETED",
            action="MERGE_COMPLETE_SET",
            taker_fee_paid=fee,
            extra={
                "hedge_fill": _quote_payload(fill),
                "detected_hedge_quote": _quote_payload(pending.detected_quote),
                "actual_hedge_latency_ms": actual_latency,
            },
        )

    def _recover_now(self, engine: ArbitrageEngine, pair: MarketPair | None, campaign: HedgeCampaign, *, reason: str) -> None:
        if pair is None:
            self.recovery_failures += 1
            self._finalize(
                campaign,
                -campaign.maker_fill_notional,
                status="HEDGE_RECOVERY_FAILURE",
                action="BOOK_PRUNED_WITH_OPEN_INVENTORY",
                taker_fee_paid=ZERO,
                extra={"recovery_reason": reason},
            )
            return
        campaign.pending_hedge = None
        campaign.pending_recovery = PendingRecovery(execute_at=time.monotonic(), reason=reason)
        self._execute_recovery(engine, pair, campaign)

    def _execute_recovery(self, engine: ArbitrageEngine, pair: MarketPair, campaign: HedgeCampaign) -> None:
        pending = campaign.pending_recovery
        if pending is None:
            return
        qty = campaign.maker_fill_qty
        hedge_book = engine.books.get(pair.token_b if campaign.hedge_side == "B" else pair.token_a)
        maker_book = engine.books.get(pair.token_a if campaign.maker_side == "A" else pair.token_b)

        completion = hedge_book.quote_buy(qty) if hedge_book else None
        completion_fee = taker_fee(completion.segments, self.settings.crypto_taker_fee_rate) if completion else ZERO
        completion_pnl = (
            qty - campaign.maker_fill_notional - completion.notional - completion_fee
            if completion
            else Decimal("-999999")
        )

        unwind = maker_book.quote_sell(qty) if maker_book else None
        unwind_fee = taker_fee(unwind.segments, self.settings.crypto_taker_fee_rate) if unwind else ZERO
        unwind_pnl = (
            unwind.notional - campaign.maker_fill_notional - unwind_fee
            if unwind
            else -campaign.maker_fill_notional
        )
        campaign.pending_recovery = None

        if completion is not None and completion_pnl >= unwind_pnl:
            self.recovery_completions += 1
            self._finalize(
                campaign,
                completion_pnl,
                status="HEDGE_RECOVERY_COMPLETE",
                action="COMPLETE_MISSING_LEG_AND_MERGE",
                taker_fee_paid=completion_fee,
                extra={
                    "recovery_reason": pending.reason,
                    "recovery_completion_quote": _quote_payload(completion),
                    "recovery_unwind_quote": _quote_payload(unwind),
                    "recovery_completion_pnl": completion_pnl,
                    "recovery_unwind_pnl": unwind_pnl,
                },
            )
            return

        self.recovery_unwinds += 1
        self._finalize(
            campaign,
            unwind_pnl,
            status="HEDGE_RECOVERY_UNWIND",
            action="UNWIND_MAKER_INVENTORY",
            taker_fee_paid=unwind_fee,
            extra={
                "recovery_reason": pending.reason,
                "recovery_completion_quote": _quote_payload(completion),
                "recovery_unwind_quote": _quote_payload(unwind),
                "recovery_completion_pnl": completion_pnl if completion else None,
                "recovery_unwind_pnl": unwind_pnl,
            },
        )

    def _cancel(self, campaign: HedgeCampaign, reason: str) -> None:
        self.cancelled += 1
        self.cooldown_until[campaign.market_id] = time.monotonic() + self.settings.hedge_requote_cooldown_ms / 1000
        self.recorder.write(
            "hedge_campaign_cancelled",
            {
                "strategy": self.strategy_name,
                "market_id": campaign.market_id,
                "slug": campaign.slug,
                "reason": reason,
                "maker_side": campaign.maker_side,
                "maker_price": campaign.maker_price,
                "shares": campaign.target_shares,
                "age_ms": Decimal(str((time.monotonic() - campaign.placed_at) * 1000)),
                "queue_ahead_remaining": campaign.queue.queue_ahead,
            },
        )
        self.campaigns.pop(campaign.market_id, None)

    def _rebate_scenarios(self, pnl: Decimal, taker_fee_paid: Decimal) -> dict[str, Decimal]:
        return {
            format(rate.normalize(), "f"): pnl + taker_fee_paid * rate
            for rate in self.settings.hedge_taker_rebate_scenarios
        }

    def _finalize(
        self,
        campaign: HedgeCampaign,
        pnl: Decimal,
        *,
        status: str,
        action: str,
        taker_fee_paid: Decimal,
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
        if campaign.regime == "EXTREME":
            self.extreme_events += 1
            self.extreme_pnl += pnl
        else:
            self.mid_events += 1
            self.mid_pnl += pnl

        summary = {
            "strategy": self.strategy_name,
            "mode": "HEDGEABLE_MAKER_TAKER",
            "finalized_at": _utc_now(),
            "market_id": campaign.market_id,
            "slug": campaign.slug,
            "regime": campaign.regime,
            "status": status,
            "action": action,
            "target_net_edge_per_share": self.edge_target,
            "completion_latency_ms": self.completion_latency_ms,
            "maker_side": campaign.maker_side,
            "hedge_side": campaign.hedge_side,
            "target_shares": campaign.target_shares,
            "maker_fill_qty": campaign.maker_fill_qty,
            "maker_price": campaign.maker_price,
            "maker_fill_notional": campaign.maker_fill_notional,
            "cancelled_unfilled_remainder": campaign.cancelled_remainder,
            "initial_queue_ahead": campaign.queue.initial_queue_ahead,
            "placement_hedge_quote": _quote_payload(campaign.placement_hedge_quote),
            "placement_hedge_fee": campaign.placement_hedge_fee,
            "placement_expected_profit_after_reserve": campaign.placement_expected_profit,
            "placement_expected_edge_after_reserve": campaign.placement_expected_edge,
            "taker_fee_paid": taker_fee_paid,
            "taker_rebate_pnl_scenarios": self._rebate_scenarios(pnl, taker_fee_paid),
            "realized_pnl": pnl,
            "equity_after": equity_after,
            **extra,
        }
        self.last_summary = summary
        self.recorder.write("hedge_execution_summary", summary)
        self.campaigns.pop(campaign.market_id, None)
        self.cooldown_until[campaign.market_id] = time.monotonic() + self.settings.hedge_requote_cooldown_ms / 1000
        log.info(
            "%s %s %s | regime=%s maker=%s@%s qty=%s pnl=%+.4f equity=%+.4f",
            self.strategy_name,
            status,
            campaign.slug,
            campaign.regime,
            campaign.maker_side,
            campaign.maker_price,
            campaign.maker_fill_qty,
            float(pnl),
            float(self.total_pnl),
        )

    def diagnostic_row(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy_name,
            "edge_target": self.edge_target,
            "latency_ms": self.completion_latency_ms,
            "equity": self.total_pnl,
            "pending": self.pending_count,
            "placed": self.placed,
            "maker_fills": self.maker_fills,
            "hedge_attempts": self.hedge_attempts,
            "hedge_successes": self.hedge_successes,
            "hedge_misses": self.hedge_misses,
            "recovery_completions": self.recovery_completions,
            "recovery_unwinds": self.recovery_unwinds,
            "cancelled": self.cancelled,
            "avg_queue": self.average_initial_queue,
            "avg_fill_ms": self.average_time_to_fill_ms,
            "avg_hedge_latency_ms": self.average_hedge_latency_ms,
            "mid_events": self.mid_events,
            "mid_pnl": self.mid_pnl,
            "extreme_events": self.extreme_events,
            "extreme_pnl": self.extreme_pnl,
            "surge_skips": self.surge_skips,
            "no_hedgeable_quote": self.no_hedgeable_quote,
            "max_drawdown": self.equity.max_drawdown,
        }


class HedgeableResearchSuite:
    """Runs edge-target x latency hedgeability-first counterfactuals."""

    def __init__(
        self,
        settings: Settings,
        recorder: JsonlRecorder,
        regime_tracker: MarketRegimeTracker,
    ) -> None:
        self.settings = settings
        self.recorder = recorder
        self.regime_tracker = regime_tracker
        self.variants = [
            HedgeableVariantEngine(
                settings,
                recorder,
                regime_tracker,
                edge_target=edge,
                completion_latency_ms=latency,
            )
            for edge in settings.hedge_net_edge_targets
            for latency in settings.hedge_completion_latencies_ms
        ]

    def on_market_update(self, engine: ArbitrageEngine, market_id: str, surge: SurgeSnapshot) -> None:
        for variant in self.variants:
            variant.on_market_update(engine, market_id, surge)

    def process_due(self, engine: ArbitrageEngine) -> None:
        for variant in self.variants:
            variant.process_due(engine)

    def diagnostic_rows(self) -> list[dict[str, Any]]:
        return [variant.diagnostic_row() for variant in self.variants]
