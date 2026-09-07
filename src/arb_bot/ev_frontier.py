from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
from typing import Any

from .config import Settings
from .discovery import MarketPhase, btc_15m_window_from_slug, market_phase
from .fees import taker_fee
from .hedgeable_research import HedgeCampaign, HedgeIntent, HedgeableVariantEngine, _edge_label, _quote_payload, _utc_now
from .maker_research import MarketRegimeTracker, QueueLeg, SurgeSnapshot
from .models import MarketPair
from .storage import JsonlRecorder
from .strategy import ArbitrageEngine
from .strategy_metrics import StrategyEquity

ZERO = Decimal("0")
ONE = Decimal("1")


def _ceil_to_tick(value: Decimal, tick: Decimal) -> Decimal:
    if value <= ZERO or tick <= ZERO:
        return ZERO
    return (value / tick).to_integral_value(rounding=ROUND_CEILING) * tick


def _pct(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator <= ZERO:
        return ZERO
    return numerator / denominator


@dataclass(slots=True)
class GhostOrder:
    market_id: str
    slug: str
    cancel_reason: str
    maker_side: str
    hedge_side: str
    shares: Decimal
    maker_price: Decimal
    edge_target: Decimal
    cancelled_at: float
    expires_at: float
    queue_ahead: Decimal
    initial_queue_ahead: Decimal
    last_trade_seen: float
    regime: str


class FrontierHedgeVariant(HedgeableVariantEngine):
    """Instrumented hedgeability-first variant used to map the EV frontier.

    It adds four things to the Phase 1.4 engine:
    - bounded hedgeability grace periods;
    - fixed-size experiments;
    - cancellation/queue diagnostics and ghost orders;
    - direct empirical P(fill), P(hedge|fill), and EV estimates.

    Ghost orders are observation-only. They never affect strategy P&L or equity.
    """

    def __init__(
        self,
        settings: Settings,
        recorder: JsonlRecorder,
        regime_tracker: MarketRegimeTracker,
        *,
        edge_target: Decimal,
        completion_latency_ms: int,
        grace_ms: int,
        fixed_size: Decimal,
        strategy_name: str,
    ) -> None:
        super().__init__(
            settings,
            recorder,
            regime_tracker,
            edge_target=edge_target,
            completion_latency_ms=completion_latency_ms,
        )
        self.strategy_name = strategy_name
        self.equity = StrategyEquity(strategy_name, recorder)
        self.grace_ms = max(0, grace_ms)
        self.fixed_size = fixed_size
        self.below_target_since: dict[str, float] = {}

        self.cancel_reasons: Counter[str] = Counter()
        self.cancel_queue_consumption_total = ZERO
        self.cancel_queue_samples = 0
        self.ghosts: dict[str, list[GhostOrder]] = {}
        self.ghost_created = 0
        self.ghost_filled = 0
        self.ghost_profitable = 0
        self.ghost_target_profitable = 0
        self.ghost_expired = 0

        self.direct_pnl_sum = ZERO
        self.direct_pnl_count = 0
        self.recovery_pnl_sum = ZERO
        self.recovery_pnl_count = 0

    def _best_intent(self, engine: ArbitrageEngine, pair: MarketPair) -> HedgeIntent | None:
        shares = self.fixed_size
        if shares <= ZERO or shares > self.settings.max_trade_shares:
            return None
        candidates = [
            self._side_intent(engine, pair, maker_side=side, shares=shares)
            for side in ("A", "B")
        ]
        candidates = [intent for intent in candidates if intent is not None]
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda item: (item.expected_profit_after_reserve, -item.queue_ahead),
        )

    def _current_hedge_state(
        self,
        engine: ArbitrageEngine,
        pair: MarketPair,
        campaign: HedgeCampaign,
    ) -> tuple[bool, Decimal | None, Decimal | None, str | None]:
        hedge_book = engine.books.get(pair.token_b if campaign.hedge_side == "B" else pair.token_a)
        if not hedge_book or not hedge_book.ready:
            return False, None, None, "NO_HEDGE_BOOK"
        quote = hedge_book.quote_buy(campaign.target_shares)
        if quote is None:
            return False, None, None, "NO_HEDGE_DEPTH"
        net, edge, _ = self._decision_economics(quote, campaign.maker_price, campaign.target_shares)
        if edge >= self.edge_target and net >= self.settings.hedge_min_expected_profit_usdc:
            return True, net, edge, None
        if edge <= -self.settings.ev_hard_loss_per_share:
            return False, net, edge, "HARD_LOSS"
        return False, net, edge, None

    def _grace_cancel_reason(
        self,
        engine: ArbitrageEngine,
        pair: MarketPair,
        campaign: HedgeCampaign,
        now: float,
    ) -> str | None:
        okay, net, edge, terminal = self._current_hedge_state(engine, pair, campaign)
        if okay:
            self.below_target_since.pop(campaign.market_id, None)
            return None
        if terminal is not None:
            return terminal
        if self.grace_ms <= 0:
            return "HEDGEABILITY_DROPPED"
        since = self.below_target_since.get(campaign.market_id)
        if since is None:
            self.below_target_since[campaign.market_id] = now
            self.recorder.write(
                "hedge_grace_entered",
                {
                    "strategy": self.strategy_name,
                    "market_id": campaign.market_id,
                    "slug": campaign.slug,
                    "grace_ms": self.grace_ms,
                    "current_net": net,
                    "current_edge_per_share": edge,
                    "hard_loss_per_share": self.settings.ev_hard_loss_per_share,
                },
            )
            return None
        if (now - since) * 1000 >= self.grace_ms:
            return "HEDGEABILITY_GRACE_EXPIRED"
        return None

    def on_market_update(self, engine: ArbitrageEngine, market_id: str, surge: SurgeSnapshot) -> None:
        if not self.settings.ev_frontier_enabled:
            return
        pair = engine.pairs.get(market_id)
        if pair:
            self._process_ghosts(engine, pair)
        if not pair or market_phase(pair) != MarketPhase.LIVE:
            return

        campaign = self.campaigns.get(market_id)
        if campaign is None:
            self._maybe_place(engine, pair, surge)
            return

        if campaign.has_fill:
            return

        fill = self._consume_maker_trade(engine, pair, campaign)
        if fill > ZERO:
            self.below_target_since.pop(market_id, None)
            self._on_maker_fill(engine, pair, campaign, fill)
            return

        if surge.active:
            self._cancel(campaign, "SURGE")
            return

        reason = self._grace_cancel_reason(engine, pair, campaign, time.monotonic())
        if reason:
            self._cancel(campaign, reason)

    def process_due(self, engine: ArbitrageEngine) -> None:
        now = time.monotonic()
        self._expire_ghosts(engine, now)
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
            reason = self._grace_cancel_reason(engine, pair, campaign, now)
            if reason:
                self._cancel(campaign, reason)

    def _cancel(self, campaign: HedgeCampaign, reason: str) -> None:
        self.cancel_reasons[reason] += 1
        consumed = max(campaign.queue.initial_queue_ahead - campaign.queue.queue_ahead, ZERO)
        consumption = _pct(consumed, campaign.queue.initial_queue_ahead)
        self.cancel_queue_consumption_total += consumption
        self.cancel_queue_samples += 1

        if (
            self.settings.hedge_ghost_enabled
            and reason not in {"WINDOW_ROLLOVER", "NEAR_EXPIRY"}
            and len(self.ghosts.get(campaign.market_id, [])) < self.settings.hedge_ghost_max_active
        ):
            ghost = GhostOrder(
                market_id=campaign.market_id,
                slug=campaign.slug,
                cancel_reason=reason,
                maker_side=campaign.maker_side,
                hedge_side=campaign.hedge_side,
                shares=campaign.target_shares,
                maker_price=campaign.maker_price,
                edge_target=self.edge_target,
                cancelled_at=time.monotonic(),
                expires_at=time.monotonic() + self.settings.hedge_ghost_horizon_ms / 1000,
                queue_ahead=campaign.queue.queue_ahead,
                initial_queue_ahead=campaign.queue.initial_queue_ahead,
                last_trade_seen=campaign.queue.last_trade_seen,
                regime=campaign.regime,
            )
            self.ghosts.setdefault(campaign.market_id, []).append(ghost)
            self.ghost_created += 1
            self.recorder.write(
                "hedge_ghost_created",
                {
                    "strategy": self.strategy_name,
                    "market_id": campaign.market_id,
                    "slug": campaign.slug,
                    "cancel_reason": reason,
                    "maker_side": campaign.maker_side,
                    "maker_price": campaign.maker_price,
                    "shares": campaign.target_shares,
                    "initial_queue_ahead": campaign.queue.initial_queue_ahead,
                    "queue_ahead_at_cancel": campaign.queue.queue_ahead,
                    "queue_consumed_fraction_at_cancel": consumption,
                    "ghost_horizon_ms": self.settings.hedge_ghost_horizon_ms,
                },
            )

        self.below_target_since.pop(campaign.market_id, None)
        super()._cancel(campaign, reason)

    def _process_ghosts(self, engine: ArbitrageEngine, pair: MarketPair) -> None:
        ghosts = self.ghosts.get(pair.market_id)
        if not ghosts:
            return
        maker_books = {"A": engine.books.get(pair.token_a), "B": engine.books.get(pair.token_b)}
        keep: list[GhostOrder] = []
        now = time.monotonic()
        for ghost in ghosts:
            if now >= ghost.expires_at:
                self._record_ghost_no_fill(ghost, "GHOST_HORIZON_EXPIRED")
                continue
            book = maker_books.get(ghost.maker_side)
            if not book or book.last_trade_monotonic <= ghost.last_trade_seen:
                keep.append(ghost)
                continue
            ghost.last_trade_seen = book.last_trade_monotonic
            if (
                book.last_trade_side != "SELL"
                or book.last_trade_price is None
                or book.last_trade_size is None
                or book.last_trade_size <= ZERO
                or book.last_trade_price > ghost.maker_price
            ):
                keep.append(ghost)
                continue

            fill = ZERO
            if book.last_trade_price < ghost.maker_price:
                fill = ghost.shares
                ghost.queue_ahead = ZERO
            else:
                traded = book.last_trade_size
                queue_used = min(ghost.queue_ahead, traded)
                ghost.queue_ahead -= queue_used
                overflow = max(traded - queue_used, ZERO)
                fill = min(ghost.shares, overflow)

            if fill <= ZERO:
                keep.append(ghost)
                continue
            self._record_ghost_fill(engine, pair, ghost, fill)
        if keep:
            self.ghosts[pair.market_id] = keep
        else:
            self.ghosts.pop(pair.market_id, None)

    def _expire_ghosts(self, engine: ArbitrageEngine, now: float) -> None:
        for market_id in list(self.ghosts):
            keep: list[GhostOrder] = []
            for ghost in self.ghosts[market_id]:
                pair = engine.pairs.get(market_id)
                if now < ghost.expires_at and pair is not None and market_phase(pair) == MarketPhase.LIVE:
                    keep.append(ghost)
                else:
                    self._record_ghost_no_fill(ghost, "GHOST_HORIZON_OR_ROLLOVER")
            if keep:
                self.ghosts[market_id] = keep
            else:
                self.ghosts.pop(market_id, None)

    def _record_ghost_no_fill(self, ghost: GhostOrder, reason: str) -> None:
        self.ghost_expired += 1
        self.recorder.write(
            "hedge_ghost_outcome",
            {
                "strategy": self.strategy_name,
                "market_id": ghost.market_id,
                "slug": ghost.slug,
                "cancel_reason": ghost.cancel_reason,
                "outcome": "NO_FILL",
                "reason": reason,
                "maker_side": ghost.maker_side,
                "maker_price": ghost.maker_price,
                "shares": ghost.shares,
                "queue_ahead_remaining": ghost.queue_ahead,
                "elapsed_ms": Decimal(str((time.monotonic() - ghost.cancelled_at) * 1000)),
            },
        )

    def _record_ghost_fill(self, engine: ArbitrageEngine, pair: MarketPair, ghost: GhostOrder, fill: Decimal) -> None:
        hedge_book = engine.books.get(pair.token_b if ghost.hedge_side == "B" else pair.token_a)
        maker_book = engine.books.get(pair.token_a if ghost.maker_side == "A" else pair.token_b)
        completion = hedge_book.quote_buy(fill) if hedge_book else None
        completion_fee = taker_fee(completion.segments, self.settings.crypto_taker_fee_rate) if completion else ZERO
        completion_pnl = (
            fill - fill * ghost.maker_price - completion.notional - completion_fee
            if completion
            else None
        )
        unwind = maker_book.quote_sell(fill) if maker_book else None
        unwind_fee = taker_fee(unwind.segments, self.settings.crypto_taker_fee_rate) if unwind else ZERO
        unwind_pnl = unwind.notional - fill * ghost.maker_price - unwind_fee if unwind else -(fill * ghost.maker_price)
        best_recovery = max(completion_pnl, unwind_pnl) if completion_pnl is not None else unwind_pnl

        self.ghost_filled += 1
        if best_recovery > ZERO:
            self.ghost_profitable += 1
        target_profit = fill * ghost.edge_target
        if completion_pnl is not None and completion_pnl >= target_profit:
            self.ghost_target_profitable += 1
        self.recorder.write(
            "hedge_ghost_outcome",
            {
                "strategy": self.strategy_name,
                "market_id": ghost.market_id,
                "slug": ghost.slug,
                "cancel_reason": ghost.cancel_reason,
                "outcome": "WOULD_FILL",
                "maker_side": ghost.maker_side,
                "hedge_side": ghost.hedge_side,
                "maker_price": ghost.maker_price,
                "fill_qty": fill,
                "elapsed_ms": Decimal(str((time.monotonic() - ghost.cancelled_at) * 1000)),
                "completion_quote": _quote_payload(completion),
                "completion_pnl": completion_pnl,
                "unwind_quote": _quote_payload(unwind),
                "unwind_pnl": unwind_pnl,
                "best_recovery_pnl": best_recovery,
                "would_be_profitable": best_recovery > ZERO,
                "would_clear_original_target": completion_pnl is not None and completion_pnl >= target_profit,
            },
        )

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
        if status == "HEDGE_COMPLETED":
            self.direct_pnl_sum += pnl
            self.direct_pnl_count += 1
        elif status.startswith("HEDGE_RECOVERY"):
            self.recovery_pnl_sum += pnl
            self.recovery_pnl_count += 1
        super()._finalize(
            campaign,
            pnl,
            status=status,
            action=action,
            taker_fee_paid=taker_fee_paid,
            extra={
                "grace_ms": self.grace_ms,
                "fixed_size": self.fixed_size,
                **extra,
            },
        )

    @property
    def fill_probability(self) -> Decimal:
        resolved = self.maker_fills + self.cancelled
        return Decimal(self.maker_fills) / Decimal(resolved) if resolved else ZERO

    @property
    def hedge_probability_given_fill(self) -> Decimal:
        return Decimal(self.hedge_successes) / Decimal(self.maker_fills) if self.maker_fills else ZERO

    @property
    def average_direct_pnl(self) -> Decimal:
        return self.direct_pnl_sum / Decimal(self.direct_pnl_count) if self.direct_pnl_count else ZERO

    @property
    def average_recovery_pnl(self) -> Decimal:
        return self.recovery_pnl_sum / Decimal(self.recovery_pnl_count) if self.recovery_pnl_count else ZERO

    @property
    def modeled_ev_per_placement(self) -> Decimal:
        p_fill = self.fill_probability
        p_hedge = self.hedge_probability_given_fill
        return p_fill * (p_hedge * self.average_direct_pnl + (ONE - p_hedge) * self.average_recovery_pnl)

    @property
    def realized_ev_per_placement(self) -> Decimal:
        return self.total_pnl / Decimal(self.placed) if self.placed else ZERO

    @property
    def average_cancel_queue_consumed(self) -> Decimal:
        return self.cancel_queue_consumption_total / Decimal(self.cancel_queue_samples) if self.cancel_queue_samples else ZERO

    def diagnostic_row(self) -> dict[str, Any]:
        row = super().diagnostic_row()
        row.update(
            {
                "family": "EV_FRONTIER",
                "grace_ms": self.grace_ms,
                "fixed_size": self.fixed_size,
                "cancel_reasons": dict(self.cancel_reasons),
                "avg_cancel_queue_consumed": self.average_cancel_queue_consumed,
                "ghost_created": self.ghost_created,
                "ghost_filled": self.ghost_filled,
                "ghost_profitable": self.ghost_profitable,
                "ghost_target_profitable": self.ghost_target_profitable,
                "ghost_expired": self.ghost_expired,
                "p_fill": self.fill_probability,
                "p_hedge_given_fill": self.hedge_probability_given_fill,
                "avg_direct_pnl": self.average_direct_pnl,
                "avg_recovery_pnl": self.average_recovery_pnl,
                "modeled_ev_per_placement": self.modeled_ev_per_placement,
                "realized_ev_per_placement": self.realized_ev_per_placement,
            }
        )
        return row


class EVFrontierSuite:
    """Focused Phase 1.5 grace and fixed-size experiments."""

    def __init__(self, settings: Settings, recorder: JsonlRecorder, regime_tracker: MarketRegimeTracker) -> None:
        self.settings = settings
        self.recorder = recorder
        self.regime_tracker = regime_tracker
        self.variants: list[FrontierHedgeVariant] = []

        grace_size = settings.ev_grace_trade_shares
        for grace in settings.ev_grace_periods_ms:
            name = f"EV-G{grace}-S{format(grace_size.normalize(), 'f')}-E{_edge_label(settings.ev_grace_edge_target)}-L{settings.ev_grace_latency_ms}"
            self.variants.append(
                FrontierHedgeVariant(
                    settings,
                    recorder,
                    regime_tracker,
                    edge_target=settings.ev_grace_edge_target,
                    completion_latency_ms=settings.ev_grace_latency_ms,
                    grace_ms=grace,
                    fixed_size=grace_size,
                    strategy_name=name,
                )
            )

        for size in settings.ev_size_candidates:
            name = f"EV-S{format(size.normalize(), 'f')}-G{settings.ev_size_grace_ms}-E{_edge_label(settings.ev_size_edge_target)}-L{settings.ev_size_latency_ms}"
            self.variants.append(
                FrontierHedgeVariant(
                    settings,
                    recorder,
                    regime_tracker,
                    edge_target=settings.ev_size_edge_target,
                    completion_latency_ms=settings.ev_size_latency_ms,
                    grace_ms=settings.ev_size_grace_ms,
                    fixed_size=size,
                    strategy_name=name,
                )
            )

    def on_market_update(self, engine: ArbitrageEngine, market_id: str, surge: SurgeSnapshot) -> None:
        for variant in self.variants:
            variant.on_market_update(engine, market_id, surge)

    def process_due(self, engine: ArbitrageEngine) -> None:
        for variant in self.variants:
            variant.process_due(engine)

    def diagnostic_rows(self) -> list[dict[str, Any]]:
        return [variant.diagnostic_row() for variant in self.variants]

    def ranked_rows(self) -> list[dict[str, Any]]:
        return sorted(
            self.diagnostic_rows(),
            key=lambda row: (row["modeled_ev_per_placement"], row["realized_ev_per_placement"]),
            reverse=True,
        )


@dataclass(slots=True)
class SplitSellCampaign:
    market_id: str
    slug: str
    target_shares: Decimal
    edge_target: Decimal
    ask_a: Decimal
    ask_b: Decimal
    leg_a: QueueLeg
    leg_b: QueueLeg
    placed_at: float
    placed_at_utc: str
    first_fill_at: float | None = None

    @property
    def full(self) -> bool:
        return self.leg_a.filled_qty >= self.target_shares and self.leg_b.filled_qty >= self.target_shares

    @property
    def any_fill(self) -> bool:
        return self.leg_a.filled_qty > ZERO or self.leg_b.filled_qty > ZERO


class SplitSellVariant:
    """Shadow split -> passive sell both outcomes.

    The campaign starts with hypothetical equal UP/DOWN inventory created from
    `target_shares` pUSD. Both outcomes are offered passively. If both maker
    sells fill, revenue above the original split collateral is profit. If only
    one side fills, the remaining matched inventory is merged and only the
    residual unmatched token is sold as a taker during recovery.
    """

    def __init__(
        self,
        settings: Settings,
        recorder: JsonlRecorder,
        regime_tracker: MarketRegimeTracker,
        edge_target: Decimal,
    ) -> None:
        self.settings = settings
        self.recorder = recorder
        self.regime_tracker = regime_tracker
        self.edge_target = edge_target
        self.strategy_name = f"SPLITSELL-{_edge_label(edge_target)}"
        self.campaigns: dict[str, SplitSellCampaign] = {}
        self.cooldown_until: dict[str, float] = {}
        self.equity = StrategyEquity(self.strategy_name, recorder)
        self.total_pnl = ZERO
        self.placed = 0
        self.cancelled = 0
        self.first_fill_campaigns = 0
        self.completed = 0
        self.residual_exits = 0
        self.wins = 0
        self.losses = 0
        self.total_queue = ZERO
        self.total_first_fill_ms = ZERO

    def _seconds_to_expiry(self, slug: str) -> float:
        window = btc_15m_window_from_slug(slug)
        if not window:
            return 999999.0
        _, end = window
        from datetime import datetime, timezone
        return max(0.0, (end - datetime.now(timezone.utc)).total_seconds())

    def _target_asks(self, engine: ArbitrageEngine, pair: MarketPair) -> tuple[Decimal, Decimal] | None:
        a = engine.books.get(pair.token_a)
        b = engine.books.get(pair.token_b)
        if not a or not b or not a.ready or not b.ready:
            return None
        ask_a = a.best_ask()
        ask_b = b.best_ask()
        if ask_a is None or ask_b is None:
            return None
        target_total = ONE + self.edge_target
        if ask_a + ask_b >= target_total:
            return ask_a, ask_b
        shortfall = target_total - ask_a - ask_b
        ticks = int(_ceil_to_tick(shortfall, self.settings.maker_tick_size) / self.settings.maker_tick_size)
        add_a = ticks // 2
        add_b = ticks - add_a
        return (
            ask_a + self.settings.maker_tick_size * Decimal(add_a),
            ask_b + self.settings.maker_tick_size * Decimal(add_b),
        )

    def on_market_update(self, engine: ArbitrageEngine, market_id: str, surge: SurgeSnapshot) -> None:
        if not self.settings.split_sell_enabled:
            return
        pair = engine.pairs.get(market_id)
        if not pair or market_phase(pair) != MarketPhase.LIVE:
            return
        campaign = self.campaigns.get(market_id)
        if campaign is None:
            self._maybe_place(engine, pair, surge)
            return
        self._consume_trades(engine, pair, campaign)
        campaign = self.campaigns.get(market_id)
        if campaign is None:
            return
        if campaign.full:
            self._finalize_complete(campaign)
            return
        if campaign.any_fill and surge.active:
            self._exit_residual(engine, pair, campaign, "SURGE_WITH_INVENTORY")
        elif not campaign.any_fill and surge.active:
            self._cancel(campaign, "SURGE")

    def process_due(self, engine: ArbitrageEngine) -> None:
        now = time.monotonic()
        for market_id in list(self.campaigns):
            campaign = self.campaigns.get(market_id)
            if campaign is None:
                continue
            pair = engine.pairs.get(market_id)
            if pair is None or market_phase(pair) != MarketPhase.LIVE:
                if campaign.any_fill and pair is not None:
                    self._exit_residual(engine, pair, campaign, "WINDOW_ROLLOVER")
                else:
                    self._cancel(campaign, "WINDOW_ROLLOVER")
                continue
            self._consume_trades(engine, pair, campaign)
            campaign = self.campaigns.get(market_id)
            if campaign is None:
                continue
            if campaign.full:
                self._finalize_complete(campaign)
                continue
            if campaign.any_fill:
                age_ms = (now - (campaign.first_fill_at or now)) * 1000
                if age_ms >= self.settings.split_sell_inventory_timeout_ms:
                    self._exit_residual(engine, pair, campaign, "INVENTORY_TIMEOUT")
                continue
            if (now - campaign.placed_at) * 1000 >= self.settings.split_sell_max_quote_age_ms:
                self._cancel(campaign, "MAX_QUOTE_AGE")
            elif self._seconds_to_expiry(pair.slug) <= self.settings.split_sell_min_seconds_to_expiry:
                self._cancel(campaign, "NEAR_EXPIRY")

    def _maybe_place(self, engine: ArbitrageEngine, pair: MarketPair, surge: SurgeSnapshot) -> None:
        now = time.monotonic()
        if now < self.cooldown_until.get(pair.market_id, 0.0) or surge.active:
            return
        if self._seconds_to_expiry(pair.slug) <= self.settings.split_sell_min_seconds_to_expiry:
            return
        prices = self._target_asks(engine, pair)
        if prices is None:
            return
        ask_a, ask_b = prices
        a = engine.books[pair.token_a]
        b = engine.books[pair.token_b]
        shares = self.settings.split_sell_shares
        campaign = SplitSellCampaign(
            market_id=pair.market_id,
            slug=pair.slug,
            target_shares=shares,
            edge_target=self.edge_target,
            ask_a=ask_a,
            ask_b=ask_b,
            leg_a=QueueLeg(ask_a, a.asks.get(ask_a, ZERO), a.asks.get(ask_a, ZERO), last_trade_seen=a.last_trade_monotonic, last_reprice_at=now),
            leg_b=QueueLeg(ask_b, b.asks.get(ask_b, ZERO), b.asks.get(ask_b, ZERO), last_trade_seen=b.last_trade_monotonic, last_reprice_at=now),
            placed_at=now,
            placed_at_utc=_utc_now(),
        )
        self.campaigns[pair.market_id] = campaign
        self.placed += 1
        self.total_queue += campaign.leg_a.initial_queue_ahead + campaign.leg_b.initial_queue_ahead
        self.recorder.write(
            "split_sell_campaign_placed",
            {
                "strategy": self.strategy_name,
                "market_id": pair.market_id,
                "slug": pair.slug,
                "shares": shares,
                "sell_price_a": ask_a,
                "sell_price_b": ask_b,
                "combined_sale_price": ask_a + ask_b,
                "target_edge_per_share": self.edge_target,
                "queue_ahead_a": campaign.leg_a.initial_queue_ahead,
                "queue_ahead_b": campaign.leg_b.initial_queue_ahead,
            },
        )

    @staticmethod
    def _consume_sell_trade(book, leg: QueueLeg, target: Decimal) -> Decimal:
        if book.last_trade_monotonic <= leg.last_trade_seen:
            return ZERO
        leg.last_trade_seen = book.last_trade_monotonic
        if (
            book.last_trade_side != "BUY"
            or book.last_trade_price is None
            or book.last_trade_size is None
            or book.last_trade_size <= ZERO
            or book.last_trade_price < leg.price
        ):
            return ZERO
        remaining = max(target - leg.filled_qty, ZERO)
        if remaining <= ZERO:
            return ZERO
        if book.last_trade_price > leg.price:
            fill = remaining
            leg.queue_ahead = ZERO
        else:
            traded = book.last_trade_size
            queue_used = min(leg.queue_ahead, traded)
            leg.queue_ahead -= queue_used
            overflow = max(traded - queue_used, ZERO)
            fill = min(remaining, overflow)
        if fill > ZERO:
            leg.filled_qty += fill
            leg.filled_notional += fill * leg.price
        return fill

    def _consume_trades(self, engine: ArbitrageEngine, pair: MarketPair, campaign: SplitSellCampaign) -> None:
        a = engine.books.get(pair.token_a)
        b = engine.books.get(pair.token_b)
        if not a or not b:
            return
        fill_a = self._consume_sell_trade(a, campaign.leg_a, campaign.target_shares)
        fill_b = self._consume_sell_trade(b, campaign.leg_b, campaign.target_shares)
        if (fill_a > ZERO or fill_b > ZERO) and campaign.first_fill_at is None:
            campaign.first_fill_at = time.monotonic()
            self.first_fill_campaigns += 1
            self.total_first_fill_ms += Decimal(str((campaign.first_fill_at - campaign.placed_at) * 1000))
        if fill_a > ZERO or fill_b > ZERO:
            self.recorder.write(
                "split_sell_queue_fill",
                {
                    "strategy": self.strategy_name,
                    "market_id": campaign.market_id,
                    "slug": campaign.slug,
                    "fill_a": fill_a,
                    "fill_b": fill_b,
                    "cum_a": campaign.leg_a.filled_qty,
                    "cum_b": campaign.leg_b.filled_qty,
                    "queue_a_remaining": campaign.leg_a.queue_ahead,
                    "queue_b_remaining": campaign.leg_b.queue_ahead,
                },
            )

    def _finalize_complete(self, campaign: SplitSellCampaign) -> None:
        revenue = campaign.leg_a.filled_notional + campaign.leg_b.filled_notional
        pnl = revenue - campaign.target_shares
        self.completed += 1
        self._finalize(campaign, pnl, "SPLIT_SELL_COMPLETE", "BOTH_PASSIVE_SELLS_FILLED", {})

    def _exit_residual(self, engine: ArbitrageEngine, pair: MarketPair, campaign: SplitSellCampaign, reason: str) -> None:
        target = campaign.target_shares
        rem_a = max(target - campaign.leg_a.filled_qty, ZERO)
        rem_b = max(target - campaign.leg_b.filled_qty, ZERO)
        matched_remaining = min(rem_a, rem_b)
        excess_a = rem_a - matched_remaining
        excess_b = rem_b - matched_remaining
        maker_revenue = campaign.leg_a.filled_notional + campaign.leg_b.filled_notional
        merged_value = matched_remaining
        taker_revenue = ZERO
        taker_fee_paid = ZERO
        unwind_quote = None
        excess_side = None
        excess_qty = ZERO
        if excess_a > ZERO:
            excess_side = "A"
            excess_qty = excess_a
            unwind_quote = engine.books[pair.token_a].quote_sell(excess_a)
        elif excess_b > ZERO:
            excess_side = "B"
            excess_qty = excess_b
            unwind_quote = engine.books[pair.token_b].quote_sell(excess_b)
        if unwind_quote is not None:
            taker_revenue = unwind_quote.notional
            taker_fee_paid = taker_fee(unwind_quote.segments, self.settings.crypto_taker_fee_rate)
        elif excess_qty > ZERO:
            taker_revenue = ZERO
        pnl = maker_revenue + merged_value + taker_revenue - taker_fee_paid - target
        self.residual_exits += 1
        self._finalize(
            campaign,
            pnl,
            "SPLIT_SELL_RESIDUAL_EXIT",
            "MERGE_MATCHED_AND_UNWIND_EXCESS",
            {
                "reason": reason,
                "remaining_a": rem_a,
                "remaining_b": rem_b,
                "matched_remaining_merged": matched_remaining,
                "excess_side": excess_side,
                "excess_qty": excess_qty,
                "unwind_quote": _quote_payload(unwind_quote),
                "taker_fee_paid": taker_fee_paid,
            },
        )

    def _cancel(self, campaign: SplitSellCampaign, reason: str) -> None:
        self.cancelled += 1
        self.cooldown_until[campaign.market_id] = time.monotonic() + self.settings.split_sell_requote_cooldown_ms / 1000
        self.recorder.write(
            "split_sell_campaign_cancelled",
            {
                "strategy": self.strategy_name,
                "market_id": campaign.market_id,
                "slug": campaign.slug,
                "reason": reason,
                "queue_a_remaining": campaign.leg_a.queue_ahead,
                "queue_b_remaining": campaign.leg_b.queue_ahead,
            },
        )
        self.campaigns.pop(campaign.market_id, None)

    def _finalize(self, campaign: SplitSellCampaign, pnl: Decimal, status: str, action: str, extra: dict[str, Any]) -> None:
        self.total_pnl += pnl
        if pnl > ZERO:
            self.wins += 1
        elif pnl < ZERO:
            self.losses += 1
        equity_after = self.equity.apply(pnl, market_id=campaign.market_id, slug=campaign.slug, status=status, action=action)
        self.recorder.write(
            "split_sell_execution_summary",
            {
                "strategy": self.strategy_name,
                "mode": "SPLIT_PASSIVE_SELL",
                "finalized_at": _utc_now(),
                "market_id": campaign.market_id,
                "slug": campaign.slug,
                "status": status,
                "action": action,
                "shares": campaign.target_shares,
                "target_edge_per_share": campaign.edge_target,
                "sell_price_a": campaign.ask_a,
                "sell_price_b": campaign.ask_b,
                "filled_qty_a": campaign.leg_a.filled_qty,
                "filled_qty_b": campaign.leg_b.filled_qty,
                "filled_notional_a": campaign.leg_a.filled_notional,
                "filled_notional_b": campaign.leg_b.filled_notional,
                "initial_queue_ahead_a": campaign.leg_a.initial_queue_ahead,
                "initial_queue_ahead_b": campaign.leg_b.initial_queue_ahead,
                "realized_pnl": pnl,
                "equity_after": equity_after,
                **extra,
            },
        )
        self.campaigns.pop(campaign.market_id, None)
        self.cooldown_until[campaign.market_id] = time.monotonic() + self.settings.split_sell_requote_cooldown_ms / 1000

    @property
    def fill_probability(self) -> Decimal:
        resolved = self.first_fill_campaigns + self.cancelled
        return Decimal(self.first_fill_campaigns) / Decimal(resolved) if resolved else ZERO

    @property
    def completion_probability_given_fill(self) -> Decimal:
        return Decimal(self.completed) / Decimal(self.first_fill_campaigns) if self.first_fill_campaigns else ZERO

    @property
    def ev_per_placement(self) -> Decimal:
        return self.total_pnl / Decimal(self.placed) if self.placed else ZERO

    def diagnostic_row(self) -> dict[str, Any]:
        avg_queue = self.total_queue / Decimal(self.placed * 2) if self.placed else ZERO
        avg_fill_ms = self.total_first_fill_ms / Decimal(self.first_fill_campaigns) if self.first_fill_campaigns else ZERO
        return {
            "strategy": self.strategy_name,
            "equity": self.total_pnl,
            "pending": len(self.campaigns),
            "placed": self.placed,
            "first_fill_campaigns": self.first_fill_campaigns,
            "completed": self.completed,
            "residual_exits": self.residual_exits,
            "cancelled": self.cancelled,
            "wins": self.wins,
            "losses": self.losses,
            "avg_queue": avg_queue,
            "avg_first_fill_ms": avg_fill_ms,
            "p_fill": self.fill_probability,
            "p_complete_given_fill": self.completion_probability_given_fill,
            "ev_per_placement": self.ev_per_placement,
            "max_drawdown": self.equity.max_drawdown,
        }


class SplitSellResearchSuite:
    def __init__(self, settings: Settings, recorder: JsonlRecorder, regime_tracker: MarketRegimeTracker) -> None:
        self.variants = [
            SplitSellVariant(settings, recorder, regime_tracker, edge)
            for edge in settings.split_sell_edge_targets
        ]

    def on_market_update(self, engine: ArbitrageEngine, market_id: str, surge: SurgeSnapshot) -> None:
        for variant in self.variants:
            variant.on_market_update(engine, market_id, surge)

    def process_due(self, engine: ArbitrageEngine) -> None:
        for variant in self.variants:
            variant.process_due(engine)

    def diagnostic_rows(self) -> list[dict[str, Any]]:
        return [variant.diagnostic_row() for variant in self.variants]
