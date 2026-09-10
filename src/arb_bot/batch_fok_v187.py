from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from statistics import median
from typing import Any

from .discovery import MarketPhase, asset_from_slug, market_phase
from .fees import taker_fee
from .models import ExecutionQuote, MarketPair
from .profit_fok_v185 import ZERO, _quote_payload, _utc_now
from .research_context_v183 import PHASE183_RUN_ID
from .research_context_v184 import PHASE184_RUN_ID
from .research_context_v185 import PHASE185_RUN_ID
from .research_context_v186 import PHASE186_RUN_ID
from .research_context_v187 import PHASE187_RUN_ID
from .strategy_metrics import StrategyEquity


@dataclass(slots=True)
class BatchRiskBucketV187:
    attempts: int = 0
    both_filled: int = 0
    one_leg_miss: int = 0
    neither_filled: int = 0
    win_pnl: Decimal = ZERO
    miss_loss: Decimal = ZERO


class BatchRiskBookV187:
    """Empirical BFOK risk model keyed by asset, size and detected edge band.

    Only the fixed-size BFOK controls contribute observations. BFOK-EV consumes
    the resulting estimates, avoiding circular double-counting when BFOK-EV and
    a fixed-size control fire on the same market update.
    """

    def __init__(self, settings) -> None:
        self.settings = settings
        self._buckets: dict[tuple[str, Decimal, str], BatchRiskBucketV187] = defaultdict(BatchRiskBucketV187)

    def _band(self, edge: Decimal) -> str:
        for upper in self.settings.v187_ev_edge_bands:
            if edge < upper:
                return f"<{format(upper.normalize(), 'f')}"
        return f">={format(self.settings.v187_ev_edge_bands[-1].normalize(), 'f')}" if self.settings.v187_ev_edge_bands else "ALL"

    def key(self, asset: str, shares: Decimal, edge: Decimal) -> tuple[str, Decimal, str]:
        return asset, shares, self._band(edge)

    def observe(
        self,
        *,
        asset: str,
        shares: Decimal,
        detected_edge: Decimal,
        status: str,
        pnl: Decimal,
    ) -> None:
        bucket = self._buckets[self.key(asset, shares, detected_edge)]
        bucket.attempts += 1
        if status == "BOTH_FILLED":
            bucket.both_filled += 1
            if pnl > ZERO:
                bucket.win_pnl += pnl
        elif status == "ONE_LEG_MISS":
            bucket.one_leg_miss += 1
            if pnl < ZERO:
                bucket.miss_loss += -pnl
        else:
            bucket.neither_filled += 1

    def estimate(
        self,
        *,
        asset: str,
        shares: Decimal,
        detected_edge: Decimal,
        detected_pnl: Decimal,
    ) -> dict[str, Any]:
        key = self.key(asset, shares, detected_edge)
        bucket = self._buckets[key]
        prior_weight = max(self.settings.v187_ev_prior_weight, ZERO)
        prior_both = min(max(self.settings.v187_ev_prior_both_probability, ZERO), Decimal("1"))
        prior_miss = Decimal("1") - prior_both
        denom = Decimal(bucket.attempts) + prior_weight
        if denom <= ZERO:
            p_both = prior_both
            p_miss = prior_miss
        else:
            p_both = (Decimal(bucket.both_filled) + prior_both * prior_weight) / denom
            p_miss = (Decimal(bucket.one_leg_miss) + prior_miss * prior_weight) / denom

        if bucket.one_leg_miss > 0 and bucket.miss_loss > ZERO:
            expected_miss_loss = bucket.miss_loss / Decimal(bucket.one_leg_miss)
        else:
            expected_miss_loss = self.settings.v187_ev_prior_miss_loss_per_share * shares

        expected_pnl = p_both * detected_pnl - p_miss * expected_miss_loss
        return {
            "asset": asset,
            "shares": shares,
            "edge_band": key[2],
            "samples": bucket.attempts,
            "both_filled": bucket.both_filled,
            "one_leg_miss": bucket.one_leg_miss,
            "neither_filled": bucket.neither_filled,
            "p_both": p_both,
            "p_miss": p_miss,
            "expected_miss_loss": expected_miss_loss,
            "expected_pnl": expected_pnl,
            "empirical_ready": bucket.attempts >= self.settings.v187_ev_min_empirical_samples,
        }


@dataclass(slots=True)
class SharedBatchSnapshotV187:
    market_id: str
    pair: MarketPair
    started_at: float
    completed_at: float
    metrics: dict[Decimal, dict[str, Any]]


@dataclass(slots=True)
class PendingBatchFOKV187:
    pair: MarketPair
    shares: Decimal
    detected_at: float
    detected_at_utc: str
    detected_edge: Decimal
    detected_pnl: Decimal
    detected_pair_price: Decimal
    detected_coverage_a: Decimal
    detected_coverage_b: Decimal
    limit_a: Decimal
    limit_b: Decimal
    arrival_due: float
    risk_estimate: dict[str, Any] | None = None
    arrival_processed: bool = False
    fill_a: ExecutionQuote | None = None
    fill_b: ExecutionQuote | None = None
    arrival_a_ms: Decimal | None = None
    arrival_b_ms: Decimal | None = None
    recovery_due: float | None = None
    recovery_started_at: float | None = None
    filled_leg: str | None = None


class BatchFOKEngineV187:
    """Parallel two-leg FOK shadow engine.

    Detection constructs both orders from one book snapshot. Both orders share
    one modeled venue-arrival deadline and are independently FOK-validated at
    that deadline against their original per-leg price limits. This is *not*
    atomic execution: A can fill while B misses (or vice versa), and any such
    exposure is recovered with the resulting P&L booked to strategy equity.
    """

    direction = "BUY_PAIR"
    mode = "BATCH_FOK_V187"

    def __init__(
        self,
        settings,
        recorder,
        *,
        strategy: str,
        fixed_size: Decimal | None,
        risk_book: BatchRiskBookV187,
        ev_gate: bool = False,
        contributes_risk: bool = True,
    ) -> None:
        self.settings = settings
        self.recorder = recorder
        self.strategy = strategy
        self.fixed_size = fixed_size
        self.risk_book = risk_book
        self.ev_gate = ev_gate
        self.contributes_risk = contributes_risk
        self.pending: dict[str, PendingBatchFOKV187] = {}
        self.cooldown_until: dict[str, float] = {}
        self.equity = StrategyEquity(strategy, recorder)

        self.candidates = 0
        self.placements = 0
        self.both_filled = 0
        self.one_leg_miss = 0
        self.neither_filled = 0
        self.recovery_completions = 0
        self.recovery_unwinds = 0
        self.recovery_liquidity_failures = 0
        self.surge_blocks = 0
        self.edge_rejects = 0
        self.coverage_rejects = 0
        self.ev_rejects = 0
        self.lifetimes_ms: list[Decimal] = []
        self.arrival_latencies_ms: list[Decimal] = []
        self.scheduler_slippage_ms: list[Decimal] = []
        self.miss_losses_per_share: list[Decimal] = []

    def _common(self) -> dict[str, Any]:
        return {
            "phase183_run_id": PHASE183_RUN_ID,
            "phase184_run_id": PHASE184_RUN_ID,
            "phase185_run_id": PHASE185_RUN_ID,
            "phase186_run_id": PHASE186_RUN_ID,
            "phase187_run_id": PHASE187_RUN_ID,
            "strategy": self.strategy,
            "mode": self.mode,
            "direction": self.direction,
            "batch_parallel": True,
            "atomic": False,
        }

    def _book_fresh(self, book, now: float) -> bool:
        return (
            book is not None
            and book.ready
            and book.updated_monotonic > 0
            and (now - book.updated_monotonic) * 1000 <= self.settings.v187_max_book_age_ms
        )

    def _eligible_metric(self, metrics: dict[str, Any]) -> bool:
        if metrics["edge"] < self.settings.v187_detection_min_edge_per_share:
            self.edge_rejects += 1
            return False
        if metrics["coverage"] < self.settings.v187_detection_coverage_multiple:
            self.coverage_rejects += 1
            return False
        return True

    def _select_candidate(
        self,
        snapshot: SharedBatchSnapshotV187,
    ) -> tuple[Decimal, dict[str, Any], dict[str, Any] | None] | None:
        asset = asset_from_slug(snapshot.pair.slug) or "UNKNOWN"
        if self.fixed_size is not None:
            metrics = snapshot.metrics.get(self.fixed_size)
            if metrics is None or not self._eligible_metric(metrics):
                return None
            return self.fixed_size, metrics, None

        # BFOK-EV chooses the candidate with the highest current risk-adjusted
        # expected P&L, not simply the largest nominal size.
        accepted: list[tuple[Decimal, dict[str, Any], dict[str, Any]]] = []
        for shares in self.settings.v187_ev_sizes:
            metrics = snapshot.metrics.get(shares)
            if metrics is None:
                continue
            if metrics["edge"] < self.settings.v187_detection_min_edge_per_share:
                continue
            if metrics["coverage"] < self.settings.v187_detection_coverage_multiple:
                continue
            estimate = self.risk_book.estimate(
                asset=asset,
                shares=shares,
                detected_edge=metrics["edge"],
                detected_pnl=metrics["pnl"],
            )
            passed = estimate["expected_pnl"] >= self.settings.v187_ev_min_expected_pnl
            self.recorder.write(
                "batch_fok_ev_gate_v187",
                {
                    **self._common(),
                    "market_id": snapshot.pair.market_id,
                    "slug": snapshot.pair.slug,
                    "asset": asset,
                    "shares": shares,
                    "detected_edge_per_share": metrics["edge"],
                    "detected_pnl": metrics["pnl"],
                    "passed": passed,
                    **estimate,
                },
            )
            if passed:
                accepted.append((shares, metrics, estimate))

        if not accepted:
            self.ev_rejects += 1
            return None
        return max(accepted, key=lambda item: item[2]["expected_pnl"])

    def on_market_update_from_snapshot(self, snapshot: SharedBatchSnapshotV187, surge=None) -> None:
        if not self.settings.v187_batch_fok_enabled:
            return
        if self.ev_gate and not self.settings.v187_ev_enabled:
            return
        pair = snapshot.pair
        if market_phase(pair) != MarketPhase.LIVE:
            return
        now = time.monotonic()
        if pair.market_id in self.pending or now < self.cooldown_until.get(pair.market_id, 0.0):
            return
        if self.settings.v187_use_surge_gate and surge is not None and surge.active:
            self.surge_blocks += 1
            return

        selected = self._select_candidate(snapshot)
        if selected is None:
            return
        shares, metrics, risk_estimate = selected
        pending = PendingBatchFOKV187(
            pair=pair,
            shares=shares,
            detected_at=now,
            detected_at_utc=_utc_now(),
            detected_edge=metrics["edge"],
            detected_pnl=metrics["pnl"],
            detected_pair_price=metrics["pair_price"],
            detected_coverage_a=metrics["coverage_a"],
            detected_coverage_b=metrics["coverage_b"],
            limit_a=metrics["quote_a"].marginal_price,
            limit_b=metrics["quote_b"].marginal_price,
            arrival_due=now + self.settings.v187_batch_arrival_latency_ms / 1000,
            risk_estimate=risk_estimate,
        )
        self.pending[pair.market_id] = pending
        self.candidates += 1
        self.placements += 1

        base_payload = {
            **self._common(),
            "market_id": pair.market_id,
            "slug": pair.slug,
            "asset": asset_from_slug(pair.slug) or "UNKNOWN",
            "shares": shares,
            "detected_at": pending.detected_at_utc,
            "detected_edge_per_share": pending.detected_edge,
            "detected_pnl": pending.detected_pnl,
            "detected_pair_price": pending.detected_pair_price,
            "coverage_a": pending.detected_coverage_a,
            "coverage_b": pending.detected_coverage_b,
            "limit_a": pending.limit_a,
            "limit_b": pending.limit_b,
            "configured_batch_arrival_latency_ms": self.settings.v187_batch_arrival_latency_ms,
            "snapshot_build_ms": Decimal(str(max(0.0, (snapshot.completed_at - snapshot.started_at) * 1000))),
            "risk_estimate": risk_estimate,
        }
        self.recorder.write("batch_fok_candidate_v187", base_payload)
        self.recorder.write("batch_fok_submission_v187", base_payload)

    def _record_leg(
        self,
        pending: PendingBatchFOKV187,
        *,
        leg: str,
        quote: ExecutionQuote | None,
        arrival_ms: Decimal,
        reason: str | None,
    ) -> None:
        self.recorder.write(
            "batch_fok_leg_result_v187",
            {
                **self._common(),
                "market_id": pending.pair.market_id,
                "slug": pending.pair.slug,
                "asset": asset_from_slug(pending.pair.slug) or "UNKNOWN",
                "shares": pending.shares,
                "leg": leg,
                "filled": quote is not None,
                "arrival_ms": arrival_ms,
                "reason": reason,
                "fill": _quote_payload(quote),
            },
        )

    def _pair_pnl(self, pending: PendingBatchFOKV187, qa: ExecutionQuote, qb: ExecutionQuote) -> Decimal:
        fee_a = taker_fee(qa.segments, self.settings.crypto_taker_fee_rate)
        fee_b = taker_fee(qb.segments, self.settings.crypto_taker_fee_rate)
        return pending.shares - qa.notional - qb.notional - fee_a - fee_b

    def _process_arrival(self, engine, pending: PendingBatchFOKV187, now: float) -> None:
        pair = pending.pair
        book_a = engine.books.get(pair.token_a)
        book_b = engine.books.get(pair.token_b)
        fresh_a = self._book_fresh(book_a, now)
        fresh_b = self._book_fresh(book_b, now)
        qa = book_a.quote_buy(pending.shares, max_price=pending.limit_a) if fresh_a else None
        qb = book_b.quote_buy(pending.shares, max_price=pending.limit_b) if fresh_b else None

        actual_ms = Decimal(str(max(0.0, (now - pending.detected_at) * 1000)))
        slip_ms = Decimal(str(max(0.0, (now - pending.arrival_due) * 1000)))
        pending.arrival_processed = True
        pending.fill_a = qa
        pending.fill_b = qb
        pending.arrival_a_ms = actual_ms
        pending.arrival_b_ms = actual_ms
        self.arrival_latencies_ms.append(actual_ms)
        self.scheduler_slippage_ms.append(slip_ms)

        reason_a = None if qa is not None else ("BOOK_STALE_A" if not fresh_a else "FOK_LIMIT_OR_DEPTH_MISS_A")
        reason_b = None if qb is not None else ("BOOK_STALE_B" if not fresh_b else "FOK_LIMIT_OR_DEPTH_MISS_B")
        self._record_leg(pending, leg="A", quote=qa, arrival_ms=actual_ms, reason=reason_a)
        self._record_leg(pending, leg="B", quote=qb, arrival_ms=actual_ms, reason=reason_b)
        self.recorder.write(
            "batch_fok_latency_v187",
            {
                **self._common(),
                "market_id": pair.market_id,
                "slug": pair.slug,
                "asset": asset_from_slug(pair.slug) or "UNKNOWN",
                "shares": pending.shares,
                "configured_arrival_ms": self.settings.v187_batch_arrival_latency_ms,
                "actual_arrival_ms": actual_ms,
                "scheduler_slippage_ms": slip_ms,
                "leg_a_filled": qa is not None,
                "leg_b_filled": qb is not None,
            },
        )

        if qa is not None and qb is not None:
            self.both_filled += 1
            pnl = self._pair_pnl(pending, qa, qb)
            self._finalize(
                pending,
                now,
                status="BOTH_FILLED",
                action="BATCH_FOK_MERGE_COMPLETE_SET",
                pnl=pnl,
                recovery=None,
            )
            return

        if qa is None and qb is None:
            self.neither_filled += 1
            self._finalize(
                pending,
                now,
                status="NEITHER_FILLED",
                action="BATCH_FOK_NO_FILL",
                pnl=ZERO,
                recovery=None,
            )
            return

        pending.filled_leg = "A" if qa is not None else "B"
        pending.recovery_started_at = now
        pending.recovery_due = now + self.settings.v187_recovery_latency_ms / 1000

    def _recover(self, engine, pending: PendingBatchFOKV187, now: float) -> None:
        filled_leg = pending.filled_leg
        assert filled_leg in {"A", "B"}
        filled = pending.fill_a if filled_leg == "A" else pending.fill_b
        assert filled is not None

        first_book = engine.books.get(pending.pair.token_a if filled_leg == "A" else pending.pair.token_b)
        missing_book = engine.books.get(pending.pair.token_b if filled_leg == "A" else pending.pair.token_a)
        fee_filled = taker_fee(filled.segments, self.settings.crypto_taker_fee_rate)
        completion_quote = missing_book.quote_buy(pending.shares) if missing_book is not None and missing_book.ready else None
        unwind_quote = first_book.quote_sell(pending.shares) if first_book is not None and first_book.ready else None

        completion_pnl = None
        unwind_pnl = None
        if completion_quote is not None:
            completion_fee = taker_fee(completion_quote.segments, self.settings.crypto_taker_fee_rate)
            completion_pnl = pending.shares - filled.notional - completion_quote.notional - fee_filled - completion_fee
        if unwind_quote is not None:
            unwind_fee = taker_fee(unwind_quote.segments, self.settings.crypto_taker_fee_rate)
            unwind_pnl = unwind_quote.notional - filled.notional - fee_filled - unwind_fee

        if completion_pnl is not None and (unwind_pnl is None or completion_pnl >= unwind_pnl):
            pnl = completion_pnl
            action = "BATCH_RECOVERY_COMPLETE_MISSING_LEG"
            self.recovery_completions += 1
        elif unwind_pnl is not None:
            pnl = unwind_pnl
            action = "BATCH_RECOVERY_UNWIND_FILLED_LEG"
            self.recovery_unwinds += 1
        else:
            pnl = -filled.notional - fee_filled
            action = "BATCH_RECOVERY_LIQUIDITY_FAILURE"
            self.recovery_liquidity_failures += 1

        self.one_leg_miss += 1
        if pending.shares > ZERO and pnl < ZERO:
            self.miss_losses_per_share.append((-pnl) / pending.shares)
        recovery_started = pending.recovery_started_at or now
        recovery = {
            "filled_leg": filled_leg,
            "configured_recovery_latency_ms": self.settings.v187_recovery_latency_ms,
            "actual_recovery_latency_ms": Decimal(str(max(0.0, (now - recovery_started) * 1000))),
            "completion_quote": _quote_payload(completion_quote),
            "completion_pnl": completion_pnl,
            "unwind_quote": _quote_payload(unwind_quote),
            "unwind_pnl": unwind_pnl,
            "chosen_action": action,
        }
        self.recorder.write(
            "batch_fok_recovery_v187",
            {
                **self._common(),
                "market_id": pending.pair.market_id,
                "slug": pending.pair.slug,
                "asset": asset_from_slug(pending.pair.slug) or "UNKNOWN",
                "shares": pending.shares,
                "pnl": pnl,
                **recovery,
            },
        )
        self._finalize(
            pending,
            now,
            status="ONE_LEG_MISS",
            action=action,
            pnl=pnl,
            recovery=recovery,
        )

    def _finalize(
        self,
        pending: PendingBatchFOKV187,
        now: float,
        *,
        status: str,
        action: str,
        pnl: Decimal,
        recovery: dict[str, Any] | None,
    ) -> None:
        self.pending.pop(pending.pair.market_id, None)
        self.cooldown_until[pending.pair.market_id] = now + self.settings.v187_cooldown_ms / 1000
        lifetime = Decimal(str(max(0.0, (now - pending.detected_at) * 1000)))
        self.lifetimes_ms.append(lifetime)
        realized_edge = pnl / pending.shares if pending.shares > ZERO else ZERO
        asset = asset_from_slug(pending.pair.slug) or "UNKNOWN"

        if self.contributes_risk:
            self.risk_book.observe(
                asset=asset,
                shares=pending.shares,
                detected_edge=pending.detected_edge,
                status=status,
                pnl=pnl,
            )

        equity_after = self.equity.apply(
            pnl,
            market_id=pending.pair.market_id,
            slug=pending.pair.slug,
            status=status,
            action=action,
        )
        self.recorder.write(
            "batch_fok_execution_summary_v187",
            {
                **self._common(),
                "finalized_at": _utc_now(),
                "market_id": pending.pair.market_id,
                "slug": pending.pair.slug,
                "asset": asset,
                "status": status,
                "action": action,
                "shares": pending.shares,
                "detected_edge_per_share": pending.detected_edge,
                "detected_pnl": pending.detected_pnl,
                "detected_pair_price": pending.detected_pair_price,
                "detected_coverage_a": pending.detected_coverage_a,
                "detected_coverage_b": pending.detected_coverage_b,
                "detected_limit_a": pending.limit_a,
                "detected_limit_b": pending.limit_b,
                "configured_batch_arrival_latency_ms": self.settings.v187_batch_arrival_latency_ms,
                "actual_arrival_a_ms": pending.arrival_a_ms,
                "actual_arrival_b_ms": pending.arrival_b_ms,
                "leg_a_filled": pending.fill_a is not None,
                "leg_b_filled": pending.fill_b is not None,
                "initial_execution": {
                    "leg_a": _quote_payload(pending.fill_a),
                    "leg_b": _quote_payload(pending.fill_b),
                },
                "risk_estimate": pending.risk_estimate,
                "recovery": recovery,
                "realized_pnl": pnl,
                "realized_edge_per_share": realized_edge,
                "equity_after": equity_after,
                "lifetime_ms": lifetime,
                "prepositioned_complete_set_inventory": False,
            },
        )

    def process_due(self, engine) -> None:
        now = time.monotonic()
        for market_id in list(self.pending):
            pending = self.pending.get(market_id)
            if pending is None:
                continue
            if not pending.arrival_processed:
                if now >= pending.arrival_due:
                    self._process_arrival(engine, pending, now)
                continue
            if pending.recovery_due is not None and now >= pending.recovery_due:
                self._recover(engine, pending, now)

    def diagnostic_row(self) -> dict[str, Any]:
        finalized = self.both_filled + self.one_leg_miss + self.neither_filled
        p_both = Decimal(self.both_filled) / Decimal(finalized) if finalized else ZERO
        p_miss = Decimal(self.one_leg_miss) / Decimal(finalized) if finalized else ZERO
        avg_life = sum(self.lifetimes_ms, ZERO) / Decimal(len(self.lifetimes_ms)) if self.lifetimes_ms else ZERO
        med_life = median(self.lifetimes_ms) if self.lifetimes_ms else ZERO
        avg_arrival = sum(self.arrival_latencies_ms, ZERO) / Decimal(len(self.arrival_latencies_ms)) if self.arrival_latencies_ms else ZERO
        med_arrival = median(self.arrival_latencies_ms) if self.arrival_latencies_ms else ZERO
        ev_place = self.equity.equity / Decimal(self.placements) if self.placements else ZERO
        return {
            "strategy": self.strategy,
            "mode": self.mode,
            "direction": self.direction,
            "pending": len(self.pending),
            "placements": self.placements,
            "completed": finalized,
            "both_filled": self.both_filled,
            "one_leg_miss": self.one_leg_miss,
            "neither_filled": self.neither_filled,
            "misses": self.one_leg_miss,
            "p_both": p_both,
            "p_miss": p_miss,
            "ev_per_placement": ev_place,
            "equity": self.equity.equity,
            "avg_lifetime_ms": avg_life,
            "median_lifetime_ms": med_life,
            "avg_arrival_ms": avg_arrival,
            "median_arrival_ms": med_arrival,
            "candidates": self.candidates,
            "ev_rejects": self.ev_rejects,
        }


class PreciseBatchFOKSuiteV187:
    """Shared-snapshot BFOK frontier with one earliest-deadline asyncio timer."""

    def __init__(self, settings, recorder) -> None:
        self.settings = settings
        self.recorder = recorder
        self.risk_book = BatchRiskBookV187(settings)
        self.variants: list[BatchFOKEngineV187] = []
        if settings.v187_batch_fok_enabled:
            for shares in settings.v187_batch_sizes:
                self.variants.append(
                    BatchFOKEngineV187(
                        settings,
                        recorder,
                        strategy=f"BFOK-{format(shares.normalize(), 'f')}",
                        fixed_size=shares,
                        risk_book=self.risk_book,
                        contributes_risk=True,
                    )
                )
            if settings.v187_ev_enabled:
                self.variants.append(
                    BatchFOKEngineV187(
                        settings,
                        recorder,
                        strategy="BFOK-EV",
                        fixed_size=None,
                        risk_book=self.risk_book,
                        ev_gate=True,
                        contributes_risk=False,
                    )
                )

        self._timer_handle: asyncio.TimerHandle | None = None
        self._timer_deadline: float | None = None
        self._timer_engine = None

    @staticmethod
    def _depth_at_limit(levels: dict[Decimal, Decimal], limit: Decimal) -> Decimal:
        return sum((qty for px, qty in levels.items() if px <= limit), ZERO)

    def _build_snapshot(self, engine, market_id: str) -> SharedBatchSnapshotV187 | None:
        pair = engine.pairs.get(market_id)
        if pair is None or market_phase(pair) != MarketPhase.LIVE:
            return None
        started = time.monotonic()
        book_a = engine.books.get(pair.token_a)
        book_b = engine.books.get(pair.token_b)
        metrics: dict[Decimal, dict[str, Any]] = {}
        if book_a is None or book_b is None or not book_a.ready or not book_b.ready:
            return SharedBatchSnapshotV187(market_id, pair, started, time.monotonic(), metrics)
        if (
            book_a.updated_monotonic <= 0
            or book_b.updated_monotonic <= 0
            or (started - book_a.updated_monotonic) * 1000 > self.settings.v187_max_book_age_ms
            or (started - book_b.updated_monotonic) * 1000 > self.settings.v187_max_book_age_ms
        ):
            return SharedBatchSnapshotV187(market_id, pair, started, time.monotonic(), metrics)

        sizes = sorted(set(self.settings.v187_batch_sizes) | set(self.settings.v187_ev_sizes))
        for shares in sizes:
            qa = book_a.quote_buy(shares)
            qb = book_b.quote_buy(shares)
            if qa is None or qb is None:
                continue
            fee_a = taker_fee(qa.segments, self.settings.crypto_taker_fee_rate)
            fee_b = taker_fee(qb.segments, self.settings.crypto_taker_fee_rate)
            pnl = shares - qa.notional - qb.notional - fee_a - fee_b
            coverage_a = self._depth_at_limit(book_a.asks, qa.marginal_price) / shares
            coverage_b = self._depth_at_limit(book_b.asks, qb.marginal_price) / shares
            metrics[shares] = {
                "quote_a": qa,
                "quote_b": qb,
                "fee_a": fee_a,
                "fee_b": fee_b,
                "pnl": pnl,
                "edge": pnl / shares,
                "pair_price": (qa.notional + qb.notional) / shares,
                "coverage_a": coverage_a,
                "coverage_b": coverage_b,
                "coverage": min(coverage_a, coverage_b),
            }
        return SharedBatchSnapshotV187(market_id, pair, started, time.monotonic(), metrics)

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
        if engine is None:
            return
        for variant in self.variants:
            variant.process_due(engine)
        self._arm_timer(engine)

    def on_market_update(self, engine, market_id: str, surge=None) -> None:
        snapshot = self._build_snapshot(engine, market_id)
        if snapshot is None:
            return
        for variant in self.variants:
            variant.on_market_update_from_snapshot(snapshot, surge)
        self._arm_timer(engine)

    def process_due(self, engine) -> None:
        for variant in self.variants:
            variant.process_due(engine)
        self._arm_timer(engine)

    def diagnostic_rows(self) -> list[dict[str, Any]]:
        return [variant.diagnostic_row() for variant in self.variants]

    def ranked_rows(self) -> list[dict[str, Any]]:
        rows = [row for row in self.diagnostic_rows() if row["placements"] > 0]
        return sorted(
            rows,
            key=lambda row: (row["ev_per_placement"], row["p_both"], -row["p_miss"]),
            reverse=True,
        )
