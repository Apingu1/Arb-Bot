from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from decimal import Decimal
from statistics import median
from typing import Any

from .batch_fok_v187 import SharedBatchSnapshotV187
from .discovery import asset_from_slug
from .fees import taker_fee
from .models import ExecutionQuote, MarketPair
from .profit_fok_v185 import ZERO, _utc_now
from .research_context_v189 import PHASE189_RUN_ID


@dataclass(slots=True)
class LatencyPendingV189:
    pair: MarketPair
    target_ms: int
    shares: Decimal
    detected_at: float
    detected_at_utc: str
    detected_edge: Decimal
    detected_pnl: Decimal
    coverage_a: Decimal
    coverage_b: Decimal
    limit_a: Decimal
    limit_b: Decimal
    detected_age_a_ms: Decimal | None
    detected_age_b_ms: Decimal | None
    detected_age_skew_ms: Decimal | None
    arrival_due: float
    arrival_processed: bool = False
    fill_a: ExecutionQuote | None = None
    fill_b: ExecutionQuote | None = None
    arrival_ms: Decimal | None = None
    scheduler_slippage_ms: Decimal | None = None
    arrival_edge: Decimal | None = None
    arrival_age_a_ms: Decimal | None = None
    arrival_age_b_ms: Decimal | None = None
    arrival_age_skew_ms: Decimal | None = None
    recovery_due: float | None = None
    recovery_started_at: float | None = None
    filled_leg: str | None = None


@dataclass(slots=True)
class LatencyVariantStateV189:
    target_ms: int
    pending: dict[str, LatencyPendingV189] = field(default_factory=dict)
    cooldown_until: dict[str, float] = field(default_factory=dict)
    candidates: int = 0
    completed: int = 0
    both_filled: int = 0
    one_leg_miss: int = 0
    neither_filled: int = 0
    wins: int = 0
    losses: int = 0
    flats: int = 0
    equity: Decimal = ZERO
    arrival_ms: list[Decimal] = field(default_factory=list)
    slippage_ms: list[Decimal] = field(default_factory=list)
    lifetimes_ms: list[Decimal] = field(default_factory=list)
    miss_losses_per_share: list[Decimal] = field(default_factory=list)

    @property
    def strategy(self) -> str:
        return f"BFOK-LAT{self.target_ms}"


class LatencyFrontierSuiteV189:
    """Protected complete-set latency frontier with shared detection snapshot.

    All variants use the same protected 1-share snapshot, edge, coverage, surge,
    freshness, fee model, cooldown and recovery logic. Only the target venue
    arrival delay changes. LAT0 is an immediate same-callback upper bound after
    protected detection, while LAT1/2/3/5 are scheduled shadow arrivals.
    """

    mode = "BATCH_FOK_LATENCY_V189"

    def __init__(self, settings, recorder) -> None:
        self.settings = settings
        self.recorder = recorder
        targets = sorted({max(0, int(v)) for v in settings.v189_latency_targets_ms})
        self.variants = [LatencyVariantStateV189(v) for v in targets] if settings.v189_latency_frontier_enabled else []
        self.shares = settings.v189_latency_size
        self._timer_handle: asyncio.TimerHandle | None = None
        self._timer_deadline: float | None = None
        self._timer_engine = None

    @staticmethod
    def _ages(engine, pair: MarketPair, now: float) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
        a = engine.books.get(pair.token_a)
        b = engine.books.get(pair.token_b)
        if a is None or b is None or a.updated_monotonic <= 0 or b.updated_monotonic <= 0:
            return None, None, None
        age_a = Decimal(str(max(0.0, (now - a.updated_monotonic) * 1000)))
        age_b = Decimal(str(max(0.0, (now - b.updated_monotonic) * 1000)))
        return age_a, age_b, abs(age_a - age_b)

    def _book_fresh(self, book, now: float) -> bool:
        return bool(
            book is not None
            and book.ready
            and book.updated_monotonic > 0
            and (now - book.updated_monotonic) * 1000 <= self.settings.v187_max_book_age_ms
        )

    def _arrival_market_edge(self, engine, pending: LatencyPendingV189) -> Decimal | None:
        a = engine.books.get(pending.pair.token_a)
        b = engine.books.get(pending.pair.token_b)
        if a is None or b is None or not a.ready or not b.ready:
            return None
        qa = a.quote_buy(pending.shares)
        qb = b.quote_buy(pending.shares)
        if qa is None or qb is None:
            return None
        fees = taker_fee(qa.segments, self.settings.crypto_taker_fee_rate) + taker_fee(
            qb.segments, self.settings.crypto_taker_fee_rate
        )
        pnl = pending.shares - qa.notional - qb.notional - fees
        return pnl / pending.shares if pending.shares > ZERO else ZERO

    def _pair_pnl(self, pending: LatencyPendingV189, qa: ExecutionQuote, qb: ExecutionQuote) -> Decimal:
        fees = taker_fee(qa.segments, self.settings.crypto_taker_fee_rate) + taker_fee(
            qb.segments, self.settings.crypto_taker_fee_rate
        )
        return pending.shares - qa.notional - qb.notional - fees

    def on_market_update(self, engine, market_id: str, surge, snapshot: SharedBatchSnapshotV187 | None) -> None:
        if not self.variants or snapshot is None or snapshot.market_id != market_id:
            return
        metric = snapshot.metrics.get(self.shares)
        if metric is None:
            return
        if metric["edge"] < self.settings.v187_detection_min_edge_per_share:
            return
        if metric["coverage"] < self.settings.v187_detection_coverage_multiple:
            return
        if self.settings.v187_use_surge_gate and surge is not None and surge.active:
            return

        now = time.monotonic()
        age_a, age_b, age_skew = self._ages(engine, snapshot.pair, snapshot.started_at)
        for variant in self.variants:
            if market_id in variant.pending or now < variant.cooldown_until.get(market_id, 0.0):
                continue
            pending = LatencyPendingV189(
                pair=snapshot.pair,
                target_ms=variant.target_ms,
                shares=self.shares,
                detected_at=now,
                detected_at_utc=_utc_now(),
                detected_edge=metric["edge"],
                detected_pnl=metric["pnl"],
                coverage_a=metric["coverage_a"],
                coverage_b=metric["coverage_b"],
                limit_a=metric["quote_a"].marginal_price,
                limit_b=metric["quote_b"].marginal_price,
                detected_age_a_ms=age_a,
                detected_age_b_ms=age_b,
                detected_age_skew_ms=age_skew,
                arrival_due=now + variant.target_ms / 1000,
            )
            variant.pending[market_id] = pending
            variant.candidates += 1
            self.recorder.write(
                "latency_candidate_v189",
                {
                    "phase189_run_id": PHASE189_RUN_ID,
                    "strategy": variant.strategy,
                    "mode": self.mode,
                    "market_id": market_id,
                    "slug": snapshot.pair.slug,
                    "asset": asset_from_slug(snapshot.pair.slug) or "UNKNOWN",
                    "shares": self.shares,
                    "target_latency_ms": variant.target_ms,
                    "detected_edge_per_share": metric["edge"],
                    "detected_pnl": metric["pnl"],
                    "coverage_a": metric["coverage_a"],
                    "coverage_b": metric["coverage_b"],
                    "detected_book_age_a_ms": age_a,
                    "detected_book_age_b_ms": age_b,
                    "detected_book_age_skew_ms": age_skew,
                },
            )
            if variant.target_ms == 0:
                self._process_arrival(engine, variant, pending, time.monotonic())

        self._arm_timer(engine)

    def _process_arrival(self, engine, variant: LatencyVariantStateV189, pending: LatencyPendingV189, now: float) -> None:
        if pending.arrival_processed:
            return
        a = engine.books.get(pending.pair.token_a)
        b = engine.books.get(pending.pair.token_b)
        fresh_a = self._book_fresh(a, now)
        fresh_b = self._book_fresh(b, now)
        qa = a.quote_buy(pending.shares, max_price=pending.limit_a) if fresh_a else None
        qb = b.quote_buy(pending.shares, max_price=pending.limit_b) if fresh_b else None

        actual = Decimal(str(max(0.0, (now - pending.detected_at) * 1000)))
        slip = Decimal(str(max(0.0, (now - pending.arrival_due) * 1000)))
        age_a, age_b, skew = self._ages(engine, pending.pair, now)
        pending.arrival_processed = True
        pending.fill_a = qa
        pending.fill_b = qb
        pending.arrival_ms = actual
        pending.scheduler_slippage_ms = slip
        pending.arrival_edge = self._arrival_market_edge(engine, pending)
        pending.arrival_age_a_ms = age_a
        pending.arrival_age_b_ms = age_b
        pending.arrival_age_skew_ms = skew
        variant.arrival_ms.append(actual)
        variant.slippage_ms.append(slip)

        if qa is not None and qb is not None:
            variant.both_filled += 1
            self._finalize(variant, pending, now, "BOTH_FILLED", self._pair_pnl(pending, qa, qb), "LATENCY_MERGE_COMPLETE_SET")
            return
        if qa is None and qb is None:
            variant.neither_filled += 1
            self._finalize(variant, pending, now, "NEITHER_FILLED", ZERO, "LATENCY_NO_FILL")
            return

        pending.filled_leg = "A" if qa is not None else "B"
        pending.recovery_started_at = now
        pending.recovery_due = now + self.settings.v187_recovery_latency_ms / 1000

    def _recover(self, engine, variant: LatencyVariantStateV189, pending: LatencyPendingV189, now: float) -> None:
        filled_leg = pending.filled_leg
        if filled_leg not in {"A", "B"}:
            return
        filled = pending.fill_a if filled_leg == "A" else pending.fill_b
        if filled is None:
            return
        first_book = engine.books.get(pending.pair.token_a if filled_leg == "A" else pending.pair.token_b)
        missing_book = engine.books.get(pending.pair.token_b if filled_leg == "A" else pending.pair.token_a)
        fee_filled = taker_fee(filled.segments, self.settings.crypto_taker_fee_rate)
        completion = missing_book.quote_buy(pending.shares) if missing_book is not None and missing_book.ready else None
        unwind = first_book.quote_sell(pending.shares) if first_book is not None and first_book.ready else None
        completion_pnl = None
        unwind_pnl = None
        if completion is not None:
            completion_pnl = pending.shares - filled.notional - completion.notional - fee_filled - taker_fee(
                completion.segments, self.settings.crypto_taker_fee_rate
            )
        if unwind is not None:
            unwind_pnl = unwind.notional - filled.notional - fee_filled - taker_fee(
                unwind.segments, self.settings.crypto_taker_fee_rate
            )
        if completion_pnl is not None and (unwind_pnl is None or completion_pnl >= unwind_pnl):
            pnl = completion_pnl
            action = "LATENCY_RECOVERY_COMPLETE_MISSING_LEG"
        elif unwind_pnl is not None:
            pnl = unwind_pnl
            action = "LATENCY_RECOVERY_UNWIND_FILLED_LEG"
        else:
            pnl = -filled.notional - fee_filled
            action = "LATENCY_RECOVERY_LIQUIDITY_FAILURE"
        variant.one_leg_miss += 1
        if pending.shares > ZERO and pnl < ZERO:
            variant.miss_losses_per_share.append((-pnl) / pending.shares)
        self._finalize(variant, pending, now, "ONE_LEG_MISS", pnl, action)

    def _finalize(
        self,
        variant: LatencyVariantStateV189,
        pending: LatencyPendingV189,
        now: float,
        status: str,
        pnl: Decimal,
        action: str,
    ) -> None:
        variant.pending.pop(pending.pair.market_id, None)
        variant.cooldown_until[pending.pair.market_id] = now + self.settings.v187_cooldown_ms / 1000
        variant.completed += 1
        variant.equity += pnl
        lifetime = Decimal(str(max(0.0, (now - pending.detected_at) * 1000)))
        variant.lifetimes_ms.append(lifetime)
        if pnl > ZERO:
            variant.wins += 1
        elif pnl < ZERO:
            variant.losses += 1
        else:
            variant.flats += 1

        # One compact equity event keeps the existing dashboard/session accounting
        # honest without the multiple per-leg/per-attempt rows used by BFOK-RAW.
        self.recorder.write(
            "strategy_equity",
            {
                "strategy": variant.strategy,
                "market_id": pending.pair.market_id,
                "slug": pending.pair.slug,
                "status": status,
                "action": action,
                "pnl_delta": pnl,
                "equity": variant.equity,
                "realized_events": variant.completed,
                "wins": variant.wins,
                "losses": variant.losses,
                "flats": variant.flats,
                "max_drawdown": ZERO,
            },
        )
        self.recorder.write(
            "latency_execution_v189",
            {
                "phase189_run_id": PHASE189_RUN_ID,
                "strategy": variant.strategy,
                "mode": self.mode,
                "market_id": pending.pair.market_id,
                "slug": pending.pair.slug,
                "asset": asset_from_slug(pending.pair.slug) or "UNKNOWN",
                "shares": pending.shares,
                "target_latency_ms": pending.target_ms,
                "actual_arrival_ms": pending.arrival_ms,
                "scheduler_slippage_ms": pending.scheduler_slippage_ms,
                "detected_edge_per_share": pending.detected_edge,
                "arrival_market_edge_per_share": pending.arrival_edge,
                "detected_pnl": pending.detected_pnl,
                "realized_pnl": pnl,
                "status": status,
                "action": action,
                "leg_a_filled": pending.fill_a is not None,
                "leg_b_filled": pending.fill_b is not None,
                "detected_book_age_a_ms": pending.detected_age_a_ms,
                "detected_book_age_b_ms": pending.detected_age_b_ms,
                "detected_book_age_skew_ms": pending.detected_age_skew_ms,
                "arrival_book_age_a_ms": pending.arrival_age_a_ms,
                "arrival_book_age_b_ms": pending.arrival_age_b_ms,
                "arrival_book_age_skew_ms": pending.arrival_age_skew_ms,
                "lifetime_ms": lifetime,
                "protected_detection": True,
                "atomic": False,
            },
        )

    def process_due(self, engine) -> None:
        if not self.variants:
            return
        now = time.monotonic()
        for variant in self.variants:
            for market_id in list(variant.pending):
                pending = variant.pending.get(market_id)
                if pending is None:
                    continue
                if not pending.arrival_processed:
                    if now >= pending.arrival_due:
                        self._process_arrival(engine, variant, pending, now)
                    continue
                if pending.recovery_due is not None and now >= pending.recovery_due:
                    self._recover(engine, variant, pending, now)
        self._arm_timer(engine)

    def _next_due(self) -> float | None:
        due: list[float] = []
        for variant in self.variants:
            for pending in variant.pending.values():
                if not pending.arrival_processed:
                    due.append(pending.arrival_due)
                elif pending.recovery_due is not None:
                    due.append(pending.recovery_due)
        return min(due) if due else None

    def _cancel_timer(self) -> None:
        if self._timer_handle is not None:
            self._timer_handle.cancel()
        self._timer_handle = None
        self._timer_deadline = None

    def _arm_timer(self, engine) -> None:
        self._timer_engine = engine
        target = self._next_due()
        if target is None:
            self._cancel_timer()
            return
        if (
            self._timer_handle is not None
            and not self._timer_handle.cancelled()
            and self._timer_deadline is not None
            and abs(self._timer_deadline - target) <= 0.000001
        ):
            return
        self._cancel_timer()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._timer_deadline = target
        self._timer_handle = loop.call_later(max(0.0, target - time.monotonic()), self._timer_fire)

    def _timer_fire(self) -> None:
        engine = self._timer_engine
        self._timer_handle = None
        self._timer_deadline = None
        if engine is not None:
            self.process_due(engine)

    @staticmethod
    def _med(values: list[Decimal]) -> Decimal:
        return median(values) if values else ZERO

    def diagnostic_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for variant in self.variants:
            done = variant.completed
            p_both = Decimal(variant.both_filled) / Decimal(done) if done else ZERO
            p_miss = Decimal(variant.one_leg_miss) / Decimal(done) if done else ZERO
            avg_arrival = sum(variant.arrival_ms, ZERO) / Decimal(len(variant.arrival_ms)) if variant.arrival_ms else ZERO
            avg_life = sum(variant.lifetimes_ms, ZERO) / Decimal(len(variant.lifetimes_ms)) if variant.lifetimes_ms else ZERO
            avg_miss = sum(variant.miss_losses_per_share, ZERO) / Decimal(len(variant.miss_losses_per_share)) if variant.miss_losses_per_share else ZERO
            rows.append(
                {
                    "strategy": variant.strategy,
                    "mode": self.mode,
                    "direction": "BUY_PAIR",
                    "pending": len(variant.pending),
                    "placements": variant.candidates,
                    "completed": done,
                    "both_filled": variant.both_filled,
                    "one_leg_miss": variant.one_leg_miss,
                    "neither_filled": variant.neither_filled,
                    "misses": variant.one_leg_miss,
                    "p_both": p_both,
                    "p_miss": p_miss,
                    "ev_per_placement": variant.equity / Decimal(variant.candidates) if variant.candidates else ZERO,
                    "equity": variant.equity,
                    "avg_lifetime_ms": avg_life,
                    "median_lifetime_ms": self._med(variant.lifetimes_ms),
                    "avg_arrival_ms": avg_arrival,
                    "median_arrival_ms": self._med(variant.arrival_ms),
                    "target_latency_ms": variant.target_ms,
                    "candidates": variant.candidates,
                    "shares": self.shares,
                    "avg_miss_loss_per_share": avg_miss,
                }
            )
        return rows

    def ranked_rows(self) -> list[dict[str, Any]]:
        rows = [row for row in self.diagnostic_rows() if row["placements"] > 0]
        return sorted(rows, key=lambda row: row["ev_per_placement"], reverse=True)
