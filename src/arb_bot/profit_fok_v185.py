from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from statistics import median
from typing import Any

from .discovery import MarketPhase, asset_from_slug, market_phase
from .fees import taker_fee
from .models import ExecutionQuote, MarketPair
from .research_context_v183 import PHASE183_RUN_ID
from .research_context_v184 import PHASE184_RUN_ID
from .research_context_v185 import PHASE185_RUN_ID
from .storage import JsonlRecorder
from .strategy import ArbitrageEngine
from .strategy_metrics import StrategyEquity


ZERO = Decimal("0")


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
        "segments": [{"price": s.price, "shares": s.shares} for s in quote.segments],
    }


def _avg(values: list[Decimal]) -> Decimal:
    return sum(values, ZERO) / Decimal(len(values)) if values else ZERO


@dataclass(slots=True)
class PendingProfitFOK:
    pair: MarketPair
    shares: Decimal
    detected_at: float
    detected_at_utc: str
    detected_edge: Decimal
    detected_pair_price: Decimal
    detected_coverage_a: Decimal
    detected_coverage_b: Decimal
    limit_a: Decimal
    limit_b: Decimal
    first_leg: str
    preflight_due: float
    preflight_edge: Decimal | None = None
    first_fill: ExecutionQuote | None = None
    second_fill: ExecutionQuote | None = None
    first_arrival_ms: Decimal | None = None
    second_arrival_ms: Decimal | None = None
    second_due: float | None = None
    recovery_due: float | None = None
    recovery_started_at: float | None = None


class ProfitFOKEngineV185:
    """Single canonical profit-seeking BUY complete-set shadow strategy.

    This deliberately differs from the ideal atomic benchmark:
    - only BUY_PAIR is traded; no mirrored SELL accounting;
    - the largest safe size from the configured list is selected once;
    - books must be fresh and over-covered at detection;
    - after simulated end-to-end latency, both legs are re-quoted at the
      original FOK price limits before the first leg is allowed to fill;
    - the second leg is forced into a later timer turn by a configured gap;
    - if the second leg disappears or no longer clears the profit floor, the
      first-leg exposure is recovered and the resulting loss/profit is booked.

    The result is still a shadow model, but losing one-leg outcomes are not
    discarded and no rejected opportunity contributes to strategy equity.
    """

    strategy = "PFOK"
    direction = "BUY_PAIR"

    def __init__(self, settings, recorder: JsonlRecorder) -> None:
        self.settings = settings
        self.recorder = recorder
        self.pending: dict[str, PendingProfitFOK] = {}
        self.cooldown_until: dict[str, float] = {}
        self.equity = StrategyEquity(self.strategy, recorder)

        self.candidates = 0
        self.preflight_rejects = 0
        self.placements = 0
        self.both_filled = 0
        self.one_leg_miss = 0
        self.neither_filled = 0
        self.recovery_completions = 0
        self.recovery_unwinds = 0
        self.recovery_liquidity_failures = 0
        self.surge_blocks = 0
        self.book_rejects = 0
        self.edge_rejects = 0
        self.coverage_rejects = 0
        self.detected_edges: list[Decimal] = []
        self.detected_coverages: list[Decimal] = []
        self.miss_losses_per_share: list[Decimal] = []
        self.lifetimes_ms: list[Decimal] = []
        self.size_counts: dict[Decimal, int] = {}

    def _common(self) -> dict[str, Any]:
        return {
            "phase183_run_id": PHASE183_RUN_ID,
            "phase184_run_id": PHASE184_RUN_ID,
            "phase185_run_id": PHASE185_RUN_ID,
            "strategy": self.strategy,
            "mode": "PROFIT_FOK_V185",
            "direction": self.direction,
        }

    def _book_fresh(self, book, now: float) -> bool:
        return (
            book is not None
            and book.ready
            and book.updated_monotonic > 0
            and (now - book.updated_monotonic) * 1000 <= self.settings.v185_max_book_age_ms
        )

    @staticmethod
    def _depth_at_limit(levels: dict[Decimal, Decimal], limit: Decimal) -> Decimal:
        return sum((qty for px, qty in levels.items() if px <= limit), ZERO)

    def _pair_metrics(
        self,
        engine: ArbitrageEngine,
        pair: MarketPair,
        shares: Decimal,
        now: float,
        *,
        limit_a: Decimal | None = None,
        limit_b: Decimal | None = None,
    ) -> dict[str, Any] | None:
        a = engine.books.get(pair.token_a)
        b = engine.books.get(pair.token_b)
        if not self._book_fresh(a, now) or not self._book_fresh(b, now):
            self.book_rejects += 1
            return None

        qa = a.quote_buy(shares, max_price=limit_a)
        qb = b.quote_buy(shares, max_price=limit_b)
        if qa is None or qb is None:
            return None
        fee_a = taker_fee(qa.segments, self.settings.crypto_taker_fee_rate)
        fee_b = taker_fee(qb.segments, self.settings.crypto_taker_fee_rate)
        pnl = shares - qa.notional - qb.notional - fee_a - fee_b
        edge = pnl / shares
        coverage_a = self._depth_at_limit(a.asks, qa.marginal_price) / shares
        coverage_b = self._depth_at_limit(b.asks, qb.marginal_price) / shares
        return {
            "quote_a": qa,
            "quote_b": qb,
            "fee_a": fee_a,
            "fee_b": fee_b,
            "pnl": pnl,
            "edge": edge,
            "pair_price": (qa.notional + qb.notional) / shares,
            "coverage_a": coverage_a,
            "coverage_b": coverage_b,
            "coverage": min(coverage_a, coverage_b),
        }

    def _candidate(self, engine: ArbitrageEngine, pair: MarketPair, now: float) -> tuple[Decimal, dict[str, Any]] | None:
        saw_quote = False
        saw_edge = False
        for shares in sorted(self.settings.v185_profit_sizes, reverse=True):
            metrics = self._pair_metrics(engine, pair, shares, now)
            if metrics is None:
                continue
            saw_quote = True
            if metrics["edge"] < self.settings.v185_detection_min_edge_per_share:
                continue
            saw_edge = True
            if metrics["coverage"] < self.settings.v185_detection_coverage_multiple:
                continue
            return shares, metrics
        if saw_quote and not saw_edge:
            self.edge_rejects += 1
        elif saw_edge:
            self.coverage_rejects += 1
        return None

    def on_market_update(self, engine: ArbitrageEngine, market_id: str, surge=None) -> None:
        if not self.settings.v185_profit_fok_enabled:
            return
        now = time.monotonic()
        pair = engine.pairs.get(market_id)
        if pair is None or market_phase(pair) != MarketPhase.LIVE:
            return
        if market_id in self.pending or now < self.cooldown_until.get(market_id, 0.0):
            return
        if self.settings.v185_use_surge_gate and surge is not None and surge.active:
            self.surge_blocks += 1
            return

        found = self._candidate(engine, pair, now)
        if found is None:
            return
        shares, metrics = found
        first_leg = "A" if metrics["coverage_a"] <= metrics["coverage_b"] else "B"
        pending = PendingProfitFOK(
            pair=pair,
            shares=shares,
            detected_at=now,
            detected_at_utc=_utc_now(),
            detected_edge=metrics["edge"],
            detected_pair_price=metrics["pair_price"],
            detected_coverage_a=metrics["coverage_a"],
            detected_coverage_b=metrics["coverage_b"],
            limit_a=metrics["quote_a"].marginal_price,
            limit_b=metrics["quote_b"].marginal_price,
            first_leg=first_leg,
            preflight_due=now + self.settings.v185_base_latency_ms / 1000,
        )
        self.pending[market_id] = pending
        self.candidates += 1
        self.detected_edges.append(metrics["edge"])
        self.detected_coverages.append(metrics["coverage"])
        self.recorder.write(
            "profit_fok_candidate_v185",
            {
                **self._common(),
                "market_id": pair.market_id,
                "slug": pair.slug,
                "asset": asset_from_slug(pair.slug) or "UNKNOWN",
                "shares": shares,
                "detected_at": pending.detected_at_utc,
                "detected_edge_per_share": metrics["edge"],
                "detected_pair_price": metrics["pair_price"],
                "coverage_a": metrics["coverage_a"],
                "coverage_b": metrics["coverage_b"],
                "first_leg": first_leg,
                "limit_a": pending.limit_a,
                "limit_b": pending.limit_b,
            },
        )

    def _abort_preflight(self, pending: PendingProfitFOK, now: float, reason: str, metrics: dict[str, Any] | None = None) -> None:
        self.pending.pop(pending.pair.market_id, None)
        self.cooldown_until[pending.pair.market_id] = now + self.settings.v185_cooldown_ms / 1000
        self.preflight_rejects += 1
        self.neither_filled += 1
        self.lifetimes_ms.append(Decimal(str(max(0.0, (now - pending.detected_at) * 1000))))
        payload = {
            **self._common(),
            "market_id": pending.pair.market_id,
            "slug": pending.pair.slug,
            "asset": asset_from_slug(pending.pair.slug) or "UNKNOWN",
            "shares": pending.shares,
            "reason": reason,
            "actual_preflight_latency_ms": Decimal(str(max(0.0, (now - pending.detected_at) * 1000))),
            "detected_edge_per_share": pending.detected_edge,
        }
        if metrics is not None:
            payload.update({
                "preflight_edge_per_share": metrics["edge"],
                "preflight_pair_price": metrics["pair_price"],
                "preflight_coverage_a": metrics["coverage_a"],
                "preflight_coverage_b": metrics["coverage_b"],
            })
        self.recorder.write("profit_fok_preflight_reject_v185", payload)

    def _process_preflight(self, engine: ArbitrageEngine, pending: PendingProfitFOK, now: float) -> None:
        metrics = self._pair_metrics(
            engine,
            pending.pair,
            pending.shares,
            now,
            limit_a=pending.limit_a,
            limit_b=pending.limit_b,
        )
        if metrics is None:
            self._abort_preflight(pending, now, "NO_FULL_SIZE_OR_STALE_BOOK")
            return
        if metrics["edge"] < self.settings.v185_preflight_min_edge_per_share:
            self._abort_preflight(pending, now, "EDGE_DECAYED_BEFORE_FIRST_LEG", metrics)
            return
        if metrics["coverage"] < self.settings.v185_preflight_coverage_multiple:
            self._abort_preflight(pending, now, "DEPTH_DECAYED_BEFORE_FIRST_LEG", metrics)
            return

        pending.preflight_edge = metrics["edge"]
        pending.first_leg = "A" if metrics["coverage_a"] <= metrics["coverage_b"] else "B"
        pending.first_fill = metrics["quote_a"] if pending.first_leg == "A" else metrics["quote_b"]
        pending.first_arrival_ms = Decimal(str(max(0.0, (now - pending.detected_at) * 1000)))
        pending.second_due = now + self.settings.v185_leg_gap_ms / 1000
        self.placements += 1
        self.size_counts[pending.shares] = self.size_counts.get(pending.shares, 0) + 1

        self.recorder.write(
            "dual_fok_attempt_placed",
            {
                **self._common(),
                "market_id": pending.pair.market_id,
                "slug": pending.pair.slug,
                "shares": pending.shares,
                "target_edge_per_share": self.settings.v185_final_min_edge_per_share,
                "detected_edge_per_share": pending.detected_edge,
                "detected_pair_price": pending.detected_pair_price,
                "detected_coverage_multiple": min(pending.detected_coverage_a, pending.detected_coverage_b),
                "coverage_multiple": self.settings.v185_detection_coverage_multiple,
                "stability_ms": 0,
                "base_latency_ms": self.settings.v185_base_latency_ms,
                "arrival_skew_ms": self.settings.v185_leg_gap_ms,
                "first_leg": pending.first_leg,
                "limit_a": pending.limit_a,
                "limit_b": pending.limit_b,
                "preflight_edge_per_share": metrics["edge"],
                "actual_first_arrival_ms": pending.first_arrival_ms,
            },
        )
        self._record_leg(pending, pending.first_leg, pending.first_fill, pending.first_arrival_ms)

    def _record_leg(self, pending: PendingProfitFOK, leg: str, fill: ExecutionQuote | None, arrival_ms: Decimal | None, *, reason: str | None = None) -> None:
        self.recorder.write(
            "profit_fok_leg_result_v185",
            {
                **self._common(),
                "market_id": pending.pair.market_id,
                "slug": pending.pair.slug,
                "shares": pending.shares,
                "leg": leg,
                "filled": fill is not None,
                "reason": reason,
                "actual_arrival_ms": arrival_ms,
                "fill": _quote_payload(fill),
            },
        )

    def _prospective_pnl(self, pending: PendingProfitFOK, second: ExecutionQuote) -> Decimal:
        assert pending.first_fill is not None
        fee_first = taker_fee(pending.first_fill.segments, self.settings.crypto_taker_fee_rate)
        fee_second = taker_fee(second.segments, self.settings.crypto_taker_fee_rate)
        return pending.shares - pending.first_fill.notional - second.notional - fee_first - fee_second

    def _process_second_leg(self, engine: ArbitrageEngine, pending: PendingProfitFOK, now: float) -> None:
        second_leg = "B" if pending.first_leg == "A" else "A"
        book = engine.books.get(pending.pair.token_b if second_leg == "B" else pending.pair.token_a)
        if not self._book_fresh(book, now):
            quote = None
            reason = "SECOND_BOOK_STALE"
        else:
            limit = pending.limit_b if second_leg == "B" else pending.limit_a
            quote = book.quote_buy(pending.shares, max_price=limit)
            reason = None if quote is not None else "SECOND_FOK_LIMIT_MISS"

        arrival = Decimal(str(max(0.0, (now - pending.detected_at) * 1000)))
        if quote is not None:
            prospective = self._prospective_pnl(pending, quote)
            if prospective / pending.shares < self.settings.v185_final_min_edge_per_share:
                quote = None
                reason = "SECOND_LEG_PROFIT_FLOOR_BLOCK"

        pending.second_arrival_ms = arrival
        pending.second_fill = quote
        self._record_leg(pending, second_leg, quote, arrival, reason=reason)
        if quote is not None:
            self.both_filled += 1
            self._finalize_both(pending, now)
            return

        pending.recovery_started_at = now
        pending.recovery_due = now + self.settings.v185_recovery_latency_ms / 1000

    def _finalize_both(self, pending: PendingProfitFOK, now: float) -> None:
        assert pending.first_fill is not None and pending.second_fill is not None
        pnl = self._prospective_pnl(pending, pending.second_fill)
        self._finalize(
            pending,
            now,
            status="BOTH_FILLED",
            action="MERGE_COMPLETE_SET",
            pnl=pnl,
            recovery=None,
        )

    def _recover(self, engine: ArbitrageEngine, pending: PendingProfitFOK, now: float) -> None:
        assert pending.first_fill is not None
        first_book = engine.books.get(pending.pair.token_a if pending.first_leg == "A" else pending.pair.token_b)
        other_book = engine.books.get(pending.pair.token_b if pending.first_leg == "A" else pending.pair.token_a)
        filled = pending.first_fill
        shares = pending.shares
        fee_filled = taker_fee(filled.segments, self.settings.crypto_taker_fee_rate)

        completion_quote = other_book.quote_buy(shares) if other_book is not None and other_book.ready else None
        unwind_quote = first_book.quote_sell(shares) if first_book is not None and first_book.ready else None
        completion_pnl = None
        unwind_pnl = None
        if completion_quote is not None:
            fee = taker_fee(completion_quote.segments, self.settings.crypto_taker_fee_rate)
            completion_pnl = shares - filled.notional - completion_quote.notional - fee_filled - fee
        if unwind_quote is not None:
            fee = taker_fee(unwind_quote.segments, self.settings.crypto_taker_fee_rate)
            unwind_pnl = unwind_quote.notional - filled.notional - fee_filled - fee

        if completion_pnl is not None and (unwind_pnl is None or completion_pnl >= unwind_pnl):
            pnl = completion_pnl
            action = "RECOVERY_COMPLETE_MISSING_LEG"
            self.recovery_completions += 1
        elif unwind_pnl is not None:
            pnl = unwind_pnl
            action = "RECOVERY_UNWIND_FIRST_LEG"
            self.recovery_unwinds += 1
        else:
            pnl = -filled.notional - fee_filled
            action = "RECOVERY_LIQUIDITY_FAILURE"
            self.recovery_liquidity_failures += 1

        self.one_leg_miss += 1
        if shares > ZERO:
            self.miss_losses_per_share.append(max(-pnl, ZERO) / shares)
        recovery_started = pending.recovery_started_at or now
        recovery = {
            "filled_leg": pending.first_leg,
            "configured_recovery_latency_ms": self.settings.v185_recovery_latency_ms,
            "actual_recovery_latency_ms": Decimal(str(max(0.0, (now - recovery_started) * 1000))),
            "completion_quote": _quote_payload(completion_quote),
            "completion_pnl": completion_pnl,
            "unwind_quote": _quote_payload(unwind_quote),
            "unwind_pnl": unwind_pnl,
            "chosen_action": action,
        }
        self._finalize(pending, now, status="ONE_LEG_MISS", action=action, pnl=pnl, recovery=recovery)

    def _finalize(
        self,
        pending: PendingProfitFOK,
        now: float,
        *,
        status: str,
        action: str,
        pnl: Decimal,
        recovery: dict[str, Any] | None,
    ) -> None:
        self.pending.pop(pending.pair.market_id, None)
        self.cooldown_until[pending.pair.market_id] = now + self.settings.v185_cooldown_ms / 1000
        lifetime = Decimal(str(max(0.0, (now - pending.detected_at) * 1000)))
        self.lifetimes_ms.append(lifetime)
        equity_after = self.equity.apply(
            pnl,
            market_id=pending.pair.market_id,
            slug=pending.pair.slug,
            status=status,
            action=action,
        )
        self.recorder.write(
            "dual_fok_execution_summary",
            {
                **self._common(),
                "finalized_at": _utc_now(),
                "market_id": pending.pair.market_id,
                "slug": pending.pair.slug,
                "asset": asset_from_slug(pending.pair.slug) or "UNKNOWN",
                "status": status,
                "action": action,
                "shares": pending.shares,
                "target_edge_per_share": self.settings.v185_final_min_edge_per_share,
                "arrival_skew_ms": self.settings.v185_leg_gap_ms,
                "base_latency_ms": self.settings.v185_base_latency_ms,
                "stability_ms": 0,
                "coverage_multiple": self.settings.v185_detection_coverage_multiple,
                "detected_edge_per_share": pending.detected_edge,
                "detected_pair_price": pending.detected_pair_price,
                "detected_coverage_multiple": min(pending.detected_coverage_a, pending.detected_coverage_b),
                "detected_coverage_a": pending.detected_coverage_a,
                "detected_coverage_b": pending.detected_coverage_b,
                "detected_limit_a": pending.limit_a,
                "detected_limit_b": pending.limit_b,
                "first_leg": pending.first_leg,
                "preflight_edge_per_share": pending.preflight_edge,
                "actual_arrival_a_ms": pending.first_arrival_ms if pending.first_leg == "A" else pending.second_arrival_ms,
                "actual_arrival_b_ms": pending.first_arrival_ms if pending.first_leg == "B" else pending.second_arrival_ms,
                "initial_execution": {
                    "leg_a": _quote_payload(pending.first_fill if pending.first_leg == "A" else pending.second_fill),
                    "leg_b": _quote_payload(pending.first_fill if pending.first_leg == "B" else pending.second_fill),
                },
                "leg_a_filled": (pending.first_leg == "A" and pending.first_fill is not None) or (pending.first_leg == "B" and pending.second_fill is not None),
                "leg_b_filled": (pending.first_leg == "B" and pending.first_fill is not None) or (pending.first_leg == "A" and pending.second_fill is not None),
                "recovery": recovery,
                "realized_pnl": pnl,
                "equity_after": equity_after,
                "lifetime_ms": lifetime,
                "prepositioned_complete_set_inventory": False,
            },
        )

    def process_due(self, engine: ArbitrageEngine) -> None:
        now = time.monotonic()
        for market_id in list(self.pending):
            pending = self.pending.get(market_id)
            if pending is None:
                continue
            if pending.first_fill is None:
                if now >= pending.preflight_due:
                    self._process_preflight(engine, pending, now)
                continue
            if pending.second_fill is None and pending.recovery_due is None:
                if pending.second_due is not None and now >= pending.second_due:
                    self._process_second_leg(engine, pending, now)
                continue
            if pending.recovery_due is not None and now >= pending.recovery_due:
                self._recover(engine, pending, now)

    def diagnostic_row(self) -> dict[str, Any]:
        finalized = self.both_filled + self.one_leg_miss
        p_both = Decimal(self.both_filled) / Decimal(finalized) if finalized else ZERO
        p_miss = Decimal(self.one_leg_miss) / Decimal(finalized) if finalized else ZERO
        size_label = "/".join(format(v.normalize(), "f") for v in sorted(self.settings.v185_profit_sizes))
        return {
            "strategy": self.strategy,
            "direction": self.direction,
            "shares": size_label,
            "edge_target": self.settings.v185_detection_min_edge_per_share,
            "arrival_skew_ms": self.settings.v185_leg_gap_ms,
            "coverage_multiple": self.settings.v185_detection_coverage_multiple,
            "stability_ms": 0,
            "base_latency_ms": self.settings.v185_base_latency_ms,
            "pending": len(self.pending),
            "opportunities": self.candidates,
            "lifetime_samples": len(self.lifetimes_ms),
            "avg_lifetime_ms": _avg(self.lifetimes_ms),
            "median_lifetime_ms": Decimal(str(median(self.lifetimes_ms))) if self.lifetimes_ms else ZERO,
            "placements": self.placements,
            "both_filled": self.both_filled,
            "one_leg_miss": self.one_leg_miss,
            "neither_filled": self.neither_filled,
            "p_both": p_both,
            "p_miss": p_miss,
            "recovery_completions": self.recovery_completions,
            "recovery_unwinds": self.recovery_unwinds,
            "recovery_liquidity_failures": self.recovery_liquidity_failures,
            "avg_detected_edge": _avg(self.detected_edges),
            "avg_detected_coverage": _avg(self.detected_coverages),
            "avg_miss_loss_per_share": _avg(self.miss_losses_per_share),
            "equity": self.equity.equity,
            "ev_per_placement": self.equity.equity / Decimal(self.placements) if self.placements else ZERO,
            "max_drawdown": self.equity.max_drawdown,
            "surge_blocks": self.surge_blocks,
            "edge_rejects": self.edge_rejects,
            "coverage_rejects": self.coverage_rejects,
            "stability_waits": 0,
            "book_rejects": self.book_rejects,
            "preflight_rejects": self.preflight_rejects,
            "size_counts": {str(k): v for k, v in self.size_counts.items()},
        }


class ProfitFOKSuiteV185:
    def __init__(self, settings, recorder: JsonlRecorder) -> None:
        self.settings = settings
        self.engine = ProfitFOKEngineV185(settings, recorder)
        self.variants = [self.engine]

    def on_market_update(self, engine: ArbitrageEngine, market_id: str, surge=None) -> None:
        self.engine.on_market_update(engine, market_id, surge)

    def process_due(self, engine: ArbitrageEngine) -> None:
        self.engine.process_due(engine)

    def diagnostic_rows(self) -> list[dict[str, Any]]:
        return [self.engine.diagnostic_row()]

    def ranked_rows(self) -> list[dict[str, Any]]:
        row = self.engine.diagnostic_row()
        return [row] if row["placements"] > 0 else []
