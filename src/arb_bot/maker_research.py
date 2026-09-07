from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
from typing import Any

from .config import Settings
from .discovery import MarketPhase, btc_15m_window_from_slug, market_phase
from .fees import taker_fee
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


def _ceil_ticks(value: Decimal, tick: Decimal) -> int:
    if value <= ZERO:
        return 0
    return int((value / tick).to_integral_value(rounding=ROUND_CEILING))


def target_bids(best_a: Decimal, best_b: Decimal, target_pair: Decimal, tick: Decimal) -> tuple[Decimal, Decimal]:
    """Return passive bids whose combined cost is <= the experiment target.

    The required discount versus the current best-bid pair is split across both
    outcomes in tick increments. This deliberately avoids choosing a side from a
    directional signal: the research question is whether deeper complete-set
    quoting improves expectancy, not whether we can predict BTC direction.
    """

    pair = best_a + best_b
    if pair <= target_pair:
        return best_a, best_b

    ticks = _ceil_ticks(pair - target_pair, tick)
    cut_a = ticks // 2
    cut_b = ticks - cut_a
    bid_a = max(tick, best_a - tick * Decimal(cut_a))
    bid_b = max(tick, best_b - tick * Decimal(cut_b))

    # If a low-priced leg hit the floor, put any remaining discount on the
    # other leg so the target is still respected where mathematically possible.
    while bid_a + bid_b > target_pair and bid_a > tick:
        bid_a -= tick
    while bid_a + bid_b > target_pair and bid_b > tick:
        bid_b -= tick
    return bid_a, bid_b


@dataclass(slots=True)
class SurgeSnapshot:
    active: bool
    reasons: tuple[str, ...]
    move_1s: Decimal = ZERO
    move_3s: Decimal = ZERO
    updates_per_second: int = 0


class MarketRegimeTracker:
    """Short-horizon volatility / update-intensity detector for maker safety."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.history: dict[str, deque[tuple[float, Decimal]]] = {}
        self.pause_until: dict[str, float] = {}
        self.last_snapshot: dict[str, SurgeSnapshot] = {}

    def observe(self, engine: ArbitrageEngine, market_id: str) -> SurgeSnapshot:
        now = time.monotonic()
        pair = engine.pairs.get(market_id)
        if not pair:
            return SurgeSnapshot(False, ())
        a = engine.books.get(pair.token_a)
        if not a or not a.ready or a.best_bid() is None or a.best_ask() is None:
            return self.last_snapshot.get(market_id, SurgeSnapshot(False, ()))

        midpoint = (a.best_bid() + a.best_ask()) / Decimal("2")
        series = self.history.setdefault(market_id, deque())
        series.append((now, midpoint))
        while series and series[0][0] < now - 3.2:
            series.popleft()

        last_1s = [(ts, px) for ts, px in series if ts >= now - 1.0]
        last_3s = [(ts, px) for ts, px in series if ts >= now - 3.0]
        move_1s = abs(midpoint - last_1s[0][1]) if last_1s else ZERO
        move_3s = abs(midpoint - last_3s[0][1]) if last_3s else ZERO
        update_rate = len(last_1s)

        reasons: list[str] = []
        if move_1s >= self.settings.surge_move_1s:
            reasons.append("MOVE_1S")
        if move_3s >= self.settings.surge_move_3s:
            reasons.append("MOVE_3S")
        if update_rate >= self.settings.surge_updates_per_second:
            reasons.append("UPDATE_RATE")

        if reasons:
            self.pause_until[market_id] = max(
                self.pause_until.get(market_id, 0.0),
                now + self.settings.surge_pause_ms / 1000,
            )

        active = now < self.pause_until.get(market_id, 0.0)
        snapshot = SurgeSnapshot(active, tuple(reasons), move_1s, move_3s, update_rate)
        self.last_snapshot[market_id] = snapshot
        return snapshot

    def current(self, market_id: str) -> SurgeSnapshot:
        now = time.monotonic()
        prior = self.last_snapshot.get(market_id, SurgeSnapshot(False, ()))
        if now < self.pause_until.get(market_id, 0.0):
            return SurgeSnapshot(True, prior.reasons or ("PAUSE_WINDOW",), prior.move_1s, prior.move_3s, prior.updates_per_second)
        return SurgeSnapshot(False, (), prior.move_1s, prior.move_3s, prior.updates_per_second)


@dataclass(slots=True)
class QueueLeg:
    price: Decimal
    initial_queue_ahead: Decimal
    queue_ahead: Decimal
    filled_qty: Decimal = ZERO
    filled_notional: Decimal = ZERO
    last_trade_seen: float = 0.0
    last_reprice_at: float = 0.0

    @property
    def average_fill_price(self) -> Decimal | None:
        if self.filled_qty <= ZERO:
            return None
        return self.filled_notional / self.filled_qty


@dataclass(slots=True)
class PendingTakerCompletion:
    missing_leg: str
    shares: Decimal
    detected_at: float
    detected_at_utc: str
    execute_at: float
    max_price: Decimal
    detected_quote: ExecutionQuote
    expected_net_profit: Decimal
    expected_net_edge_per_share: Decimal


@dataclass(slots=True)
class VariantCampaign:
    market_id: str
    slug: str
    target_pair: Decimal
    shares: Decimal
    placed_at: float
    placed_at_utc: str
    leg_a: QueueLeg
    leg_b: QueueLeg
    first_fill_at: float | None = None
    first_fill_qty: Decimal = ZERO
    last_fill_at: float | None = None
    taker_fees: Decimal = ZERO
    completion: PendingTakerCompletion | None = None

    @property
    def any_fill(self) -> bool:
        return self.leg_a.filled_qty > ZERO or self.leg_b.filled_qty > ZERO

    @property
    def full(self) -> bool:
        return self.leg_a.filled_qty >= self.shares and self.leg_b.filled_qty >= self.shares

    @property
    def matched_qty(self) -> Decimal:
        return min(self.leg_a.filled_qty, self.leg_b.filled_qty)

    @property
    def exposure_leg(self) -> str | None:
        if self.leg_a.filled_qty > self.leg_b.filled_qty:
            return "A"
        if self.leg_b.filled_qty > self.leg_a.filled_qty:
            return "B"
        return None

    @property
    def exposure_qty(self) -> Decimal:
        return abs(self.leg_a.filled_qty - self.leg_b.filled_qty)


@dataclass(slots=True)
class MakerRiskStats:
    filled_campaigns: int = 0
    completed_campaigns: int = 0
    inventory_exits: int = 0
    cumulative_inventory_loss: Decimal = ZERO
    cumulative_inventory_loss_per_share: Decimal = ZERO

    def observe_complete(self) -> None:
        self.filled_campaigns += 1
        self.completed_campaigns += 1

    def observe_inventory_exit(self, pnl: Decimal, shares: Decimal) -> None:
        self.filled_campaigns += 1
        self.inventory_exits += 1
        loss = max(-pnl, ZERO)
        self.cumulative_inventory_loss += loss
        if shares > ZERO:
            self.cumulative_inventory_loss_per_share += loss / shares

    @property
    def inventory_probability(self) -> Decimal:
        if self.filled_campaigns <= 0:
            return ZERO
        return Decimal(self.inventory_exits) / Decimal(self.filled_campaigns)

    @property
    def average_inventory_loss_per_share(self) -> Decimal:
        if self.inventory_exits <= 0:
            return ZERO
        return self.cumulative_inventory_loss_per_share / Decimal(self.inventory_exits)

    @property
    def estimated_reserve_per_share(self) -> Decimal:
        return self.inventory_probability * self.average_inventory_loss_per_share


class QueueAwareVariantEngine:
    """One queue-aware MAKER-nn or HYBRID-nn experiment."""

    def __init__(
        self,
        settings: Settings,
        recorder: JsonlRecorder,
        regime: MarketRegimeTracker,
        *,
        mode: str,
        target_pair: Decimal,
    ) -> None:
        self.settings = settings
        self.recorder = recorder
        self.regime = regime
        self.mode = mode.upper()
        self.target_pair = target_pair
        self.strategy_name = f"{self.mode}-{int(target_pair * 100):02d}"
        self.shares = settings.maker_trade_shares if self.mode == "MAKER" else settings.hybrid_trade_shares
        self.campaigns: dict[str, VariantCampaign] = {}
        self.cooldown_until: dict[str, float] = {}
        self.equity = StrategyEquity(self.strategy_name, recorder)
        self.total_pnl = ZERO
        self.placed = 0
        self.cancelled = 0
        self.completed = 0
        self.inventory_exits = 0
        self.partial_exits = 0
        self.completion_attempts = 0
        self.completion_successes = 0
        self.completion_misses = 0
        self.reprices = 0
        self.surge_skips = 0
        self.risk_skips = 0
        self.queue_fill_events = 0
        self.total_initial_queue = ZERO
        self.first_fill_campaigns = 0
        self.total_time_to_first_fill_ms = Decimal("0")
        self.interleg_samples = 0
        self.total_interleg_ms = Decimal("0")
        self.risk = MakerRiskStats()
        self.recent_inventory_exits: deque[float] = deque()
        self.last_summary: dict[str, Any] | None = None

    @property
    def pending_count(self) -> int:
        return len(self.campaigns)

    @property
    def average_initial_queue(self) -> Decimal:
        if self.placed <= 0:
            return ZERO
        return self.total_initial_queue / Decimal(self.placed * 2)

    @property
    def average_time_to_first_fill_ms(self) -> Decimal:
        if self.first_fill_campaigns <= 0:
            return ZERO
        return self.total_time_to_first_fill_ms / Decimal(self.first_fill_campaigns)

    @property
    def average_interleg_ms(self) -> Decimal:
        if self.interleg_samples <= 0:
            return ZERO
        return self.total_interleg_ms / Decimal(self.interleg_samples)

    def _local_toxic(self, now: float) -> bool:
        window = self.settings.surge_one_sided_window_seconds
        while self.recent_inventory_exits and self.recent_inventory_exits[0] < now - window:
            self.recent_inventory_exits.popleft()
        return len(self.recent_inventory_exits) >= self.settings.surge_one_sided_count

    def on_market_update(self, engine: ArbitrageEngine, market_id: str, surge: SurgeSnapshot) -> None:
        enabled = self.settings.maker_enabled if self.mode == "MAKER" else self.settings.hybrid_enabled
        if not enabled:
            return
        pair = engine.pairs.get(market_id)
        if not pair or market_phase(pair) != MarketPhase.LIVE:
            return

        campaign = self.campaigns.get(market_id)
        if campaign is None:
            self._maybe_place(engine, pair, surge)
            return

        self._consume_new_trades(engine, pair, campaign)
        campaign = self.campaigns.get(market_id)
        if campaign is None:
            return
        if campaign.full:
            self._finalize_complete(campaign)
            return
        if self.mode == "HYBRID" and campaign.any_fill:
            self._manage_hybrid(engine, pair, campaign, surge)

    def process_due(self, engine: ArbitrageEngine) -> None:
        now = time.monotonic()
        for market_id in list(self.campaigns):
            campaign = self.campaigns.get(market_id)
            if campaign is None:
                continue
            pair = engine.pairs.get(market_id)
            if pair is None or market_phase(pair) != MarketPhase.LIVE:
                if campaign.any_fill:
                    self._exit_inventory(engine, pair, campaign, reason="WINDOW_ROLLOVER")
                else:
                    self._cancel(campaign, "WINDOW_ROLLOVER")
                continue

            surge = self.regime.current(market_id)
            self._consume_new_trades(engine, pair, campaign)
            campaign = self.campaigns.get(market_id)
            if campaign is None:
                continue
            if campaign.full:
                self._finalize_complete(campaign)
                continue

            if campaign.completion and campaign.completion.execute_at <= now:
                self._execute_taker_completion(engine, pair, campaign)
                campaign = self.campaigns.get(market_id)
                if campaign is None:
                    continue

            if campaign.any_fill:
                if self.mode == "HYBRID":
                    self._manage_hybrid(engine, pair, campaign, surge)
                first_fill_age_ms = (now - (campaign.first_fill_at or now)) * 1000
                timeout = (
                    self.settings.hybrid_inventory_timeout_ms
                    if self.mode == "HYBRID"
                    else self.settings.maker_inventory_timeout_ms
                )
                if market_id in self.campaigns and first_fill_age_ms >= timeout:
                    self._exit_inventory(engine, pair, campaign, reason="INVENTORY_TIMEOUT")
                elif self.mode == "MAKER" and market_id in self.campaigns and surge.active:
                    self._exit_inventory(engine, pair, campaign, reason="SURGE_WITH_INVENTORY")
                continue

            # No fill yet: preserve queue unless the quote becomes materially
            # stale, unsafe, too old, or too close to expiry.
            if surge.active or self._local_toxic(now):
                self._cancel(campaign, "SURGE_OR_RECENT_TOXICITY")
                continue
            if (now - campaign.placed_at) * 1000 >= self.settings.maker_max_quote_age_ms:
                self._cancel(campaign, "MAX_QUOTE_AGE")
                continue
            if self._seconds_to_expiry(pair.slug) <= self.settings.maker_min_seconds_to_expiry:
                self._cancel(campaign, "NEAR_EXPIRY")
                continue

            a = engine.books.get(pair.token_a)
            b = engine.books.get(pair.token_b)
            if not a or not b or a.best_bid() is None or b.best_bid() is None:
                continue
            desired_a, desired_b = target_bids(
                a.best_bid(), b.best_bid(), self.target_pair, self.settings.maker_tick_size
            )
            threshold = self.settings.maker_tick_size * Decimal(self.settings.maker_reprice_ticks)
            if abs(desired_a - campaign.leg_a.price) >= threshold or abs(desired_b - campaign.leg_b.price) >= threshold:
                self._cancel(campaign, "MATERIAL_QUOTE_DRIFT")

    def _seconds_to_expiry(self, slug: str) -> float:
        window = btc_15m_window_from_slug(slug)
        if not window:
            return 999999.0
        _, end = window
        return max(0.0, (end - datetime.now(timezone.utc)).total_seconds())

    def _maybe_place(self, engine: ArbitrageEngine, pair: MarketPair, surge: SurgeSnapshot) -> None:
        now = time.monotonic()
        if now < self.cooldown_until.get(pair.market_id, 0.0):
            return
        if surge.active or self._local_toxic(now):
            self.surge_skips += 1
            return
        if self._seconds_to_expiry(pair.slug) <= self.settings.maker_min_seconds_to_expiry:
            return

        a = engine.books.get(pair.token_a)
        b = engine.books.get(pair.token_b)
        if not a or not b or not a.ready or not b.ready:
            return
        best_a = a.best_bid()
        best_b = b.best_bid()
        if best_a is None or best_b is None:
            return
        bid_a, bid_b = target_bids(best_a, best_b, self.target_pair, self.settings.maker_tick_size)
        gross_edge = ONE - bid_a - bid_b
        if gross_edge < self.settings.maker_min_gross_edge_per_share:
            return
        if (
            self.settings.maker_use_empirical_risk_gate
            and self.risk.filled_campaigns >= self.settings.maker_empirical_risk_min_samples
            and gross_edge <= self.risk.estimated_reserve_per_share
        ):
            self.risk_skips += 1
            return

        queue_a = a.bids.get(bid_a, ZERO)
        queue_b = b.bids.get(bid_b, ZERO)
        campaign = VariantCampaign(
            market_id=pair.market_id,
            slug=pair.slug,
            target_pair=self.target_pair,
            shares=self.shares,
            placed_at=now,
            placed_at_utc=_utc_now(),
            leg_a=QueueLeg(bid_a, queue_a, queue_a, last_trade_seen=a.last_trade_monotonic, last_reprice_at=now),
            leg_b=QueueLeg(bid_b, queue_b, queue_b, last_trade_seen=b.last_trade_monotonic, last_reprice_at=now),
        )
        self.campaigns[pair.market_id] = campaign
        self.placed += 1
        self.total_initial_queue += queue_a + queue_b
        self.recorder.write(
            "maker_variant_campaign_placed",
            {
                "strategy": self.strategy_name,
                "mode": self.mode,
                "placed_at": campaign.placed_at_utc,
                "market_id": pair.market_id,
                "slug": pair.slug,
                "target_pair": self.target_pair,
                "shares": self.shares,
                "best_bid_a_at_placement": best_a,
                "best_bid_b_at_placement": best_b,
                "maker_bid_a": bid_a,
                "maker_bid_b": bid_b,
                "combined_bid_cost": bid_a + bid_b,
                "gross_edge_per_share": gross_edge,
                "initial_queue_ahead_a": queue_a,
                "initial_queue_ahead_b": queue_b,
                "fill_model": "QUEUE_AHEAD_CONSUMED_BY_SELL_VOLUME_OR_TRADE_THROUGH",
            },
        )

    def _consume_leg_trade(self, book: TokenBook, leg: QueueLeg, shares: Decimal) -> Decimal:
        if book.last_trade_monotonic <= leg.last_trade_seen:
            return ZERO
        leg.last_trade_seen = book.last_trade_monotonic
        if (
            book.last_trade_side != "SELL"
            or book.last_trade_price is None
            or book.last_trade_size is None
            or book.last_trade_size <= ZERO
        ):
            return ZERO
        if book.last_trade_price > leg.price:
            return ZERO

        remaining = max(shares - leg.filled_qty, ZERO)
        if remaining <= ZERO:
            return ZERO

        if book.last_trade_price < leg.price:
            # A trade below our resting buy limit implies the market traded
            # through our price. Treat the remaining order as filled, but still
            # credit the conservative limit price rather than price improvement.
            fill = remaining
            leg.queue_ahead = ZERO
        else:
            traded = book.last_trade_size
            queue_consumed = min(leg.queue_ahead, traded)
            leg.queue_ahead -= queue_consumed
            overflow = max(traded - queue_consumed, ZERO)
            fill = min(remaining, overflow)

        if fill > ZERO:
            leg.filled_qty += fill
            leg.filled_notional += fill * leg.price
        return fill

    def _consume_new_trades(self, engine: ArbitrageEngine, pair: MarketPair, campaign: VariantCampaign) -> None:
        a = engine.books.get(pair.token_a)
        b = engine.books.get(pair.token_b)
        if not a or not b:
            return
        before_a = campaign.leg_a.filled_qty
        before_b = campaign.leg_b.filled_qty
        fill_a = self._consume_leg_trade(a, campaign.leg_a, campaign.shares)
        fill_b = self._consume_leg_trade(b, campaign.leg_b, campaign.shares)
        now = time.monotonic()

        for label, fill, leg, book in (
            ("A", fill_a, campaign.leg_a, a),
            ("B", fill_b, campaign.leg_b, b),
        ):
            if fill <= ZERO:
                continue
            self.queue_fill_events += 1
            if campaign.first_fill_at is None:
                campaign.first_fill_at = now
                campaign.first_fill_qty = fill
                self.first_fill_campaigns += 1
                self.total_time_to_first_fill_ms += Decimal(str((now - campaign.placed_at) * 1000))
            elif campaign.last_fill_at is not None and before_a != before_b and campaign.leg_a.filled_qty == campaign.leg_b.filled_qty:
                self.interleg_samples += 1
                self.total_interleg_ms += Decimal(str((now - campaign.last_fill_at) * 1000))
            campaign.last_fill_at = now
            self.recorder.write(
                "maker_variant_queue_fill",
                {
                    "strategy": self.strategy_name,
                    "filled_at": _utc_now(),
                    "market_id": campaign.market_id,
                    "slug": campaign.slug,
                    "leg": label,
                    "fill_qty": fill,
                    "cumulative_fill_qty": leg.filled_qty,
                    "maker_price": leg.price,
                    "queue_ahead_remaining": leg.queue_ahead,
                    "confirming_trade_price": book.last_trade_price,
                    "confirming_trade_size": book.last_trade_size,
                    "confirming_trade_timestamp": book.last_trade_timestamp,
                },
            )

        if (before_a > ZERO or before_b > ZERO) and campaign.leg_a.filled_qty == campaign.leg_b.filled_qty and campaign.leg_a.filled_qty > ZERO:
            if campaign.last_fill_at is not None and campaign.first_fill_at is not None:
                delta = Decimal(str((campaign.last_fill_at - campaign.first_fill_at) * 1000))
                if delta > ZERO:
                    self.interleg_samples += 1
                    self.total_interleg_ms += delta

    def _finalize_complete(self, campaign: VariantCampaign) -> None:
        pnl = campaign.shares - campaign.leg_a.filled_notional - campaign.leg_b.filled_notional - campaign.taker_fees
        self.completed += 1
        self.risk.observe_complete()
        status = "BOTH_MAKER_FILLED" if campaign.taker_fees == ZERO else "MAKER_PLUS_TAKER_COMPLETED"
        self._finalize(campaign, pnl, status=status, action="MERGE_COMPLETE_SET", extra={})

    def _matched_profit(self, campaign: VariantCampaign) -> Decimal:
        matched = campaign.matched_qty
        if matched <= ZERO:
            return ZERO
        avg_a = campaign.leg_a.average_fill_price
        avg_b = campaign.leg_b.average_fill_price
        if avg_a is None or avg_b is None:
            return ZERO
        return matched * (ONE - avg_a - avg_b)

    def _exposure_cost(self, campaign: VariantCampaign) -> tuple[str | None, Decimal, Decimal]:
        leg = campaign.exposure_leg
        qty = campaign.exposure_qty
        if leg == "A":
            avg = campaign.leg_a.average_fill_price or ZERO
        elif leg == "B":
            avg = campaign.leg_b.average_fill_price or ZERO
        else:
            avg = ZERO
        return leg, qty, avg

    def _unwind_quote(self, engine: ArbitrageEngine, pair: MarketPair, campaign: VariantCampaign) -> tuple[ExecutionQuote | None, Decimal]:
        leg, qty, _ = self._exposure_cost(campaign)
        if leg is None or qty <= ZERO:
            return None, ZERO
        book = engine.books.get(pair.token_a if leg == "A" else pair.token_b)
        quote = book.quote_sell(qty) if book else None
        fee = taker_fee(quote.segments, self.settings.crypto_taker_fee_rate) if quote else ZERO
        return quote, fee

    def _exit_inventory(self, engine: ArbitrageEngine, pair: MarketPair | None, campaign: VariantCampaign, *, reason: str) -> None:
        matched_pnl = self._matched_profit(campaign)
        exposure_leg, exposure_qty, exposure_avg = self._exposure_cost(campaign)
        unwind = None
        unwind_fee = ZERO
        exposure_pnl = ZERO
        if exposure_leg and exposure_qty > ZERO and pair is not None:
            unwind, unwind_fee = self._unwind_quote(engine, pair, campaign)
            if unwind:
                exposure_pnl = unwind.notional - exposure_qty * exposure_avg - unwind_fee
            else:
                exposure_pnl = -(exposure_qty * exposure_avg)
        pnl = matched_pnl + exposure_pnl - campaign.taker_fees
        self.inventory_exits += 1
        if campaign.matched_qty > ZERO:
            self.partial_exits += 1
        self.recent_inventory_exits.append(time.monotonic())
        self.risk.observe_inventory_exit(pnl, max(campaign.shares, Decimal("1")))
        self._finalize(
            campaign,
            pnl,
            status="PARTIAL_OR_ONE_SIDED_EXIT",
            action="UNWIND_RESIDUAL_INVENTORY",
            extra={
                "reason": reason,
                "matched_qty": campaign.matched_qty,
                "matched_pnl": matched_pnl,
                "exposure_leg": exposure_leg,
                "exposure_qty": exposure_qty,
                "exposure_average_cost": exposure_avg,
                "unwind_quote": _quote_payload(unwind),
                "unwind_taker_fee": unwind_fee,
            },
        )

    def _manage_hybrid(self, engine: ArbitrageEngine, pair: MarketPair, campaign: VariantCampaign, surge: SurgeSnapshot) -> None:
        if campaign.completion is not None:
            return
        exposure_leg, exposure_qty, exposure_avg = self._exposure_cost(campaign)
        if exposure_leg is None or exposure_qty <= ZERO:
            return

        missing_leg = "B" if exposure_leg == "A" else "A"
        missing_book = engine.books.get(pair.token_b if missing_leg == "B" else pair.token_a)
        if not missing_book or not missing_book.ready:
            return

        # Reprice the missing maker leg only within the complete-set target cap.
        # Example: A acquired at 0.24 with a 0.97 target -> missing B may be
        # improved up to 0.73, never above it.
        cap = max(self.settings.maker_tick_size, self.target_pair - exposure_avg)
        best_missing_bid = missing_book.best_bid()
        if best_missing_bid is not None:
            desired = min(best_missing_bid, cap)
            leg_obj = campaign.leg_b if missing_leg == "B" else campaign.leg_a
            now = time.monotonic()
            if (
                desired != leg_obj.price
                and (now - leg_obj.last_reprice_at) * 1000 >= self.settings.hybrid_min_reprice_interval_ms
            ):
                old_price = leg_obj.price
                leg_obj.price = desired
                leg_obj.initial_queue_ahead = missing_book.bids.get(desired, ZERO)
                leg_obj.queue_ahead = leg_obj.initial_queue_ahead
                leg_obj.last_trade_seen = missing_book.last_trade_monotonic
                leg_obj.last_reprice_at = now
                self.reprices += 1
                self.recorder.write(
                    "hybrid_missing_leg_reprice",
                    {
                        "strategy": self.strategy_name,
                        "market_id": campaign.market_id,
                        "slug": campaign.slug,
                        "missing_leg": missing_leg,
                        "old_price": old_price,
                        "new_price": desired,
                        "profitability_cap": cap,
                        "queue_ahead": leg_obj.queue_ahead,
                    },
                )

        # Option 1: complete now as taker if the residual exposure remains
        # positively profitable after the single missing-leg taker fee.
        quote = missing_book.quote_buy(exposure_qty)
        if quote:
            fee = taker_fee(quote.segments, self.settings.crypto_taker_fee_rate)
            incremental_net = exposure_qty - exposure_qty * exposure_avg - quote.notional - fee
            edge = incremental_net / exposure_qty
            if edge >= self.settings.hybrid_min_net_edge_per_share and incremental_net >= self.settings.min_expected_profit_usdc:
                now = time.monotonic()
                campaign.completion = PendingTakerCompletion(
                    missing_leg=missing_leg,
                    shares=exposure_qty,
                    detected_at=now,
                    detected_at_utc=_utc_now(),
                    execute_at=now + self.settings.hybrid_completion_latency_ms / 1000,
                    max_price=quote.marginal_price,
                    detected_quote=quote,
                    expected_net_profit=incremental_net,
                    expected_net_edge_per_share=edge,
                )
                self.completion_attempts += 1
                self.recorder.write(
                    "hybrid_variant_taker_intent",
                    {
                        "strategy": self.strategy_name,
                        "market_id": campaign.market_id,
                        "slug": campaign.slug,
                        "missing_leg": missing_leg,
                        "shares": exposure_qty,
                        "maker_exposure_average_cost": exposure_avg,
                        "taker_quote": _quote_payload(quote),
                        "expected_incremental_profit": incremental_net,
                        "expected_incremental_edge": edge,
                        "configured_latency_ms": self.settings.hybrid_completion_latency_ms,
                    },
                )
                return

        # Option 2/3: compare immediate unwind with holding the capped missing
        # maker quote. We hold unless exposure breaches a defined loss limit,
        # expires, or enters SURGE; those are the cases where avoiding further
        # adverse selection is worth sacrificing queue position.
        unwind, unwind_fee = self._unwind_quote(engine, pair, campaign)
        unwind_pnl = ZERO
        if unwind:
            unwind_pnl = unwind.notional - exposure_qty * exposure_avg - unwind_fee
        else:
            unwind_pnl = -(exposure_qty * exposure_avg)
        loss_per_share = min(unwind_pnl / exposure_qty, ZERO)
        age_ms = (time.monotonic() - (campaign.first_fill_at or time.monotonic())) * 1000
        if (
            surge.active
            or loss_per_share <= -self.settings.hybrid_max_hold_loss_per_share
            or age_ms >= self.settings.hybrid_inventory_timeout_ms
        ):
            reason = "SURGE" if surge.active else "LOSS_LIMIT" if loss_per_share <= -self.settings.hybrid_max_hold_loss_per_share else "INVENTORY_TIMEOUT"
            self._exit_inventory(engine, pair, campaign, reason=reason)

    def _execute_taker_completion(self, engine: ArbitrageEngine, pair: MarketPair, campaign: VariantCampaign) -> None:
        completion = campaign.completion
        if completion is None:
            return
        book = engine.books.get(pair.token_b if completion.missing_leg == "B" else pair.token_a)
        actual_latency_ms = Decimal(str((time.monotonic() - completion.detected_at) * 1000))
        fill = book.quote_buy(completion.shares, max_price=completion.max_price) if book else None
        if fill is None:
            self.completion_misses += 1
            self.recorder.write(
                "hybrid_variant_taker_miss",
                {
                    "strategy": self.strategy_name,
                    "market_id": campaign.market_id,
                    "slug": campaign.slug,
                    "missing_leg": completion.missing_leg,
                    "shares": completion.shares,
                    "detected_quote": _quote_payload(completion.detected_quote),
                    "actual_latency_ms": actual_latency_ms,
                },
            )
            campaign.completion = None
            return

        fee = taker_fee(fill.segments, self.settings.crypto_taker_fee_rate)
        leg = campaign.leg_b if completion.missing_leg == "B" else campaign.leg_a
        leg.filled_qty += completion.shares
        leg.filled_notional += fill.notional
        campaign.taker_fees += fee
        campaign.completion = None
        self.completion_successes += 1
        self.recorder.write(
            "hybrid_variant_taker_fill",
            {
                "strategy": self.strategy_name,
                "market_id": campaign.market_id,
                "slug": campaign.slug,
                "missing_leg": completion.missing_leg,
                "shares": completion.shares,
                "fill": _quote_payload(fill),
                "taker_fee": fee,
                "actual_latency_ms": actual_latency_ms,
            },
        )
        if campaign.full:
            self._finalize_complete(campaign)

    def _cancel(self, campaign: VariantCampaign, reason: str) -> None:
        self.cancelled += 1
        self.cooldown_until[campaign.market_id] = time.monotonic() + self.settings.maker_requote_cooldown_ms / 1000
        self.recorder.write(
            "maker_variant_campaign_cancelled",
            {
                "strategy": self.strategy_name,
                "market_id": campaign.market_id,
                "slug": campaign.slug,
                "reason": reason,
                "age_ms": Decimal(str((time.monotonic() - campaign.placed_at) * 1000)),
                "queue_ahead_a": campaign.leg_a.queue_ahead,
                "queue_ahead_b": campaign.leg_b.queue_ahead,
            },
        )
        self.campaigns.pop(campaign.market_id, None)

    def _finalize(self, campaign: VariantCampaign, pnl: Decimal, *, status: str, action: str, extra: dict[str, Any]) -> None:
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
            "mode": self.mode,
            "target_pair": self.target_pair,
            "finalized_at": _utc_now(),
            "market_id": campaign.market_id,
            "slug": campaign.slug,
            "status": status,
            "action": action,
            "shares": campaign.shares,
            "maker_bid_a": campaign.leg_a.price,
            "maker_bid_b": campaign.leg_b.price,
            "filled_qty_a": campaign.leg_a.filled_qty,
            "filled_qty_b": campaign.leg_b.filled_qty,
            "filled_notional_a": campaign.leg_a.filled_notional,
            "filled_notional_b": campaign.leg_b.filled_notional,
            "matched_qty": campaign.matched_qty,
            "taker_fees": campaign.taker_fees,
            "initial_queue_ahead_a": campaign.leg_a.initial_queue_ahead,
            "initial_queue_ahead_b": campaign.leg_b.initial_queue_ahead,
            "realized_pnl": pnl,
            "equity_after": equity_after,
            "maker_inventory_probability": self.risk.inventory_probability,
            "maker_avg_inventory_loss_per_share": self.risk.average_inventory_loss_per_share,
            "maker_empirical_reserve_per_share": self.risk.estimated_reserve_per_share,
            **extra,
        }
        self.last_summary = summary
        self.recorder.write("maker_variant_execution_summary", summary)
        self.campaigns.pop(campaign.market_id, None)
        log.info(
            "%s %s %s | pnl=%+.4f pUSD equity=%+.4f",
            self.strategy_name,
            status,
            campaign.slug,
            float(pnl),
            float(self.total_pnl),
        )

    def diagnostic_row(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy_name,
            "mode": self.mode,
            "target": self.target_pair,
            "equity": self.total_pnl,
            "pending": self.pending_count,
            "placed": self.placed,
            "completed": self.completed,
            "inventory_exits": self.inventory_exits,
            "partial_exits": self.partial_exits,
            "cancelled": self.cancelled,
            "completion_attempts": self.completion_attempts,
            "completion_successes": self.completion_successes,
            "completion_misses": self.completion_misses,
            "reprices": self.reprices,
            "surge_skips": self.surge_skips,
            "risk_skips": self.risk_skips,
            "avg_queue": self.average_initial_queue,
            "avg_first_fill_ms": self.average_time_to_first_fill_ms,
            "avg_interleg_ms": self.average_interleg_ms,
            "risk_probability": self.risk.inventory_probability,
            "risk_loss_per_share": self.risk.average_inventory_loss_per_share,
            "risk_reserve": self.risk.estimated_reserve_per_share,
            "max_drawdown": self.equity.max_drawdown,
        }


class MakerResearchSuite:
    """Runs MAKER-99/98/97/96 and HYBRID-99/98/97/96 on one feed."""

    def __init__(self, settings: Settings, recorder: JsonlRecorder) -> None:
        self.settings = settings
        self.recorder = recorder
        self.regime = MarketRegimeTracker(settings)
        self.makers = [
            QueueAwareVariantEngine(settings, recorder, self.regime, mode="MAKER", target_pair=target)
            for target in settings.maker_variant_targets
        ]
        self.hybrids = [
            QueueAwareVariantEngine(settings, recorder, self.regime, mode="HYBRID", target_pair=target)
            for target in settings.maker_variant_targets
        ]
        self.variants = [*self.makers, *self.hybrids]

    def on_market_update(self, engine: ArbitrageEngine, market_id: str) -> None:
        surge = self.regime.observe(engine, market_id)
        for variant in self.variants:
            variant.on_market_update(engine, market_id, surge)

    def process_due(self, engine: ArbitrageEngine) -> None:
        for variant in self.variants:
            variant.process_due(engine)

    def diagnostic_rows(self) -> list[dict[str, Any]]:
        return [variant.diagnostic_row() for variant in self.variants]

    @property
    def maker_total_pnl(self) -> Decimal:
        return sum((variant.total_pnl for variant in self.makers), ZERO)

    @property
    def hybrid_total_pnl(self) -> Decimal:
        return sum((variant.total_pnl for variant in self.hybrids), ZERO)
