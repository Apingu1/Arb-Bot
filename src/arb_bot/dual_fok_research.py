from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from statistics import median
from typing import Any

from .config import Settings
from .discovery import MarketPhase, market_phase
from .fees import taker_fee
from .maker_research import SurgeSnapshot
from .models import ExecutionQuote, MarketPair
from .storage import JsonlRecorder
from .strategy import ArbitrageEngine
from .strategy_metrics import StrategyEquity

ZERO = Decimal("0")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _avg(values: list[Decimal]) -> Decimal:
    return sum(values, ZERO) / Decimal(len(values)) if values else ZERO


def _quote_payload(quote: ExecutionQuote | None) -> dict[str, Any] | None:
    if quote is None:
        return None
    return {
        "shares": quote.shares,
        "notional": quote.notional,
        "average_price": quote.average_price,
        "marginal_price": quote.marginal_price,
        "segments": [{"price": seg.price, "shares": seg.shares} for seg in quote.segments],
    }


@dataclass(frozen=True, slots=True)
class DualFOKVariantSpec:
    strategy: str
    direction: str
    shares: Decimal
    edge_target: Decimal
    arrival_skew_ms: int
    coverage_multiple: Decimal
    stability_ms: int


@dataclass(slots=True)
class QualificationState:
    signature: tuple[str, str]
    started_at: float
    started_at_utc: str
    peak_edge: Decimal
    min_coverage: Decimal
    armed: bool = False
    surge_block_recorded: bool = False


@dataclass(slots=True)
class PendingDualFOK:
    pair: MarketPair
    spec: DualFOKVariantSpec
    detected_at: float
    detected_at_utc: str
    detected_edge: Decimal
    detected_pair_price: Decimal
    detected_coverage: Decimal
    detected_coverage_a: Decimal
    detected_coverage_b: Decimal
    detected_limit_a: Decimal
    detected_limit_b: Decimal
    detected_quote_a: ExecutionQuote
    detected_quote_b: ExecutionQuote
    first_leg: str
    due_a: float
    due_b: float
    fill_a: ExecutionQuote | None = None
    fill_b: ExecutionQuote | None = None
    processed_a: bool = False
    processed_b: bool = False
    actual_arrival_a_ms: Decimal | None = None
    actual_arrival_b_ms: Decimal | None = None
    recovery_due: float | None = None
    recovery_started_at: float | None = None


class DualFOKVariantEngine:
    """Independent shadow variant for non-atomic dual-FOK pair execution."""

    def __init__(self, settings: Settings, recorder: JsonlRecorder, spec: DualFOKVariantSpec) -> None:
        self.settings = settings
        self.recorder = recorder
        self.spec = spec
        self.qualifying: dict[str, QualificationState] = {}
        self.pending: dict[str, PendingDualFOK] = {}
        self.cooldown_until: dict[str, float] = {}
        self.equity = StrategyEquity(spec.strategy, recorder)

        self.opportunities_started = 0
        self.opportunities_ended = 0
        self.opportunity_lifetimes_ms: list[Decimal] = []
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
        self.stability_waits = 0
        self.book_rejects = 0
        self.detected_edges: list[Decimal] = []
        self.detected_coverages: list[Decimal] = []
        self.miss_losses_per_share: list[Decimal] = []

    def _book_fresh(self, updated: float, now: float) -> bool:
        return updated > 0 and now - updated <= self.settings.dual_fok_max_book_age_ms / 1000

    @staticmethod
    def _depth_at_limit(levels: dict[Decimal, Decimal], limit: Decimal, *, buy: bool) -> Decimal:
        if buy:
            return sum((qty for px, qty in levels.items() if px <= limit), ZERO)
        return sum((qty for px, qty in levels.items() if px >= limit), ZERO)

    def _metrics(self, engine: ArbitrageEngine, market_id: str, now: float) -> dict[str, Any] | None:
        pair = engine.pairs.get(market_id)
        if not pair:
            return None
        a = engine.books.get(pair.token_a)
        b = engine.books.get(pair.token_b)
        if not a or not b or not a.ready or not b.ready:
            self.book_rejects += 1
            return None
        if not self._book_fresh(a.updated_monotonic, now) or not self._book_fresh(b.updated_monotonic, now):
            self.book_rejects += 1
            return None

        if self.spec.direction == "BUY_PAIR":
            quote_a = a.quote_buy(self.spec.shares)
            quote_b = b.quote_buy(self.spec.shares)
            if not quote_a or not quote_b:
                self.coverage_rejects += 1
                return None
            fees = taker_fee(quote_a.segments, self.settings.crypto_taker_fee_rate) + taker_fee(
                quote_b.segments, self.settings.crypto_taker_fee_rate
            )
            net = self.spec.shares - quote_a.notional - quote_b.notional - fees
            pair_price = (quote_a.notional + quote_b.notional) / self.spec.shares
            depth_a = self._depth_at_limit(a.asks, quote_a.marginal_price, buy=True)
            depth_b = self._depth_at_limit(b.asks, quote_b.marginal_price, buy=True)
        else:
            quote_a = a.quote_sell(self.spec.shares)
            quote_b = b.quote_sell(self.spec.shares)
            if not quote_a or not quote_b:
                self.coverage_rejects += 1
                return None
            fees = taker_fee(quote_a.segments, self.settings.crypto_taker_fee_rate) + taker_fee(
                quote_b.segments, self.settings.crypto_taker_fee_rate
            )
            net = quote_a.notional + quote_b.notional - self.spec.shares - fees
            pair_price = (quote_a.notional + quote_b.notional) / self.spec.shares
            depth_a = self._depth_at_limit(a.bids, quote_a.marginal_price, buy=False)
            depth_b = self._depth_at_limit(b.bids, quote_b.marginal_price, buy=False)

        edge = net / self.spec.shares
        coverage_a = depth_a / self.spec.shares
        coverage_b = depth_b / self.spec.shares
        coverage = min(coverage_a, coverage_b)
        if edge < self.spec.edge_target:
            self.edge_rejects += 1
            return None
        if coverage < self.spec.coverage_multiple:
            self.coverage_rejects += 1
            return None

        return {
            "pair": pair,
            "quote_a": quote_a,
            "quote_b": quote_b,
            "edge": edge,
            "pair_price": pair_price,
            "coverage": coverage,
            "coverage_a": coverage_a,
            "coverage_b": coverage_b,
            "signature": (str(quote_a.marginal_price), str(quote_b.marginal_price)),
        }

    def _close_qualification(self, market_id: str, now: float, reason: str) -> None:
        state = self.qualifying.pop(market_id, None)
        if state is None:
            return
        lifetime = Decimal(str(max(0.0, (now - state.started_at) * 1000)))
        self.opportunities_ended += 1
        self.opportunity_lifetimes_ms.append(lifetime)
        self.recorder.write(
            "dual_fok_opportunity_lifetime",
            {
                "strategy": self.spec.strategy,
                "direction": self.spec.direction,
                "market_id": market_id,
                "started_at": state.started_at_utc,
                "ended_at": _utc_now(),
                "lifetime_ms": lifetime,
                "peak_edge_per_share": state.peak_edge,
                "min_coverage_multiple": state.min_coverage,
                "attempt_armed": state.armed,
                "end_reason": reason,
            },
        )

    def on_market_update(
        self,
        engine: ArbitrageEngine,
        market_id: str,
        surge: SurgeSnapshot | None = None,
    ) -> None:
        now = time.monotonic()
        pair = engine.pairs.get(market_id)
        if pair is None or market_phase(pair) != MarketPhase.LIVE:
            self._close_qualification(market_id, now, "NOT_LIVE")
            return

        metrics = self._metrics(engine, market_id, now)
        if metrics is None:
            self._close_qualification(market_id, now, "NO_LONGER_QUALIFIED")
            return

        state = self.qualifying.get(market_id)
        if state is not None and state.signature != metrics["signature"]:
            self._close_qualification(market_id, now, "PRICE_LIMIT_CHANGED")
            state = None

        if state is None:
            state = QualificationState(
                signature=metrics["signature"],
                started_at=now,
                started_at_utc=_utc_now(),
                peak_edge=metrics["edge"],
                min_coverage=metrics["coverage"],
            )
            self.qualifying[market_id] = state
            self.opportunities_started += 1
            self.recorder.write(
                "dual_fok_opportunity_started",
                {
                    "strategy": self.spec.strategy,
                    "direction": self.spec.direction,
                    "market_id": market_id,
                    "slug": metrics["pair"].slug,
                    "shares": self.spec.shares,
                    "edge_target_per_share": self.spec.edge_target,
                    "detected_edge_per_share": metrics["edge"],
                    "pair_price": metrics["pair_price"],
                    "coverage_multiple": metrics["coverage"],
                    "marginal_a": metrics["quote_a"].marginal_price,
                    "marginal_b": metrics["quote_b"].marginal_price,
                    "started_at": state.started_at_utc,
                },
            )
        else:
            state.peak_edge = max(state.peak_edge, metrics["edge"])
            state.min_coverage = min(state.min_coverage, metrics["coverage"])

        if state.armed or market_id in self.pending or now < self.cooldown_until.get(market_id, 0.0):
            return

        stable_ms = (now - state.started_at) * 1000
        if stable_ms < self.spec.stability_ms:
            self.stability_waits += 1
            return

        if self.settings.dual_fok_use_surge_gate and surge is not None and surge.active:
            if not state.surge_block_recorded:
                state.surge_block_recorded = True
                self.surge_blocks += 1
                self.recorder.write(
                    "dual_fok_surge_blocked",
                    {
                        "strategy": self.spec.strategy,
                        "direction": self.spec.direction,
                        "market_id": market_id,
                        "slug": metrics["pair"].slug,
                        "reasons": surge.reasons,
                        "detected_edge_per_share": metrics["edge"],
                        "coverage_multiple": metrics["coverage"],
                    },
                )
            return

        self._place(metrics, state, now)

    def _place(self, metrics: dict[str, Any], state: QualificationState, now: float) -> None:
        pair: MarketPair = metrics["pair"]
        quote_a: ExecutionQuote = metrics["quote_a"]
        quote_b: ExecutionQuote = metrics["quote_b"]

        if self.settings.dual_fok_leg_order == "A_first":
            first_leg = "A"
        elif self.settings.dual_fok_leg_order == "B_first":
            first_leg = "B"
        else:
            first_leg = "A" if metrics["coverage_a"] <= metrics["coverage_b"] else "B"

        base_due = now + self.settings.dual_fok_base_latency_ms / 1000
        skew = self.spec.arrival_skew_ms / 1000
        due_a = base_due + (0 if first_leg == "A" else skew)
        due_b = base_due + (0 if first_leg == "B" else skew)

        self.pending[pair.market_id] = PendingDualFOK(
            pair=pair,
            spec=self.spec,
            detected_at=now,
            detected_at_utc=_utc_now(),
            detected_edge=metrics["edge"],
            detected_pair_price=metrics["pair_price"],
            detected_coverage=metrics["coverage"],
            detected_coverage_a=metrics["coverage_a"],
            detected_coverage_b=metrics["coverage_b"],
            detected_limit_a=quote_a.marginal_price,
            detected_limit_b=quote_b.marginal_price,
            detected_quote_a=quote_a,
            detected_quote_b=quote_b,
            first_leg=first_leg,
            due_a=due_a,
            due_b=due_b,
        )
        state.armed = True
        self.placements += 1
        self.detected_edges.append(metrics["edge"])
        self.detected_coverages.append(metrics["coverage"])
        self.recorder.write(
            "dual_fok_attempt_placed",
            {
                "strategy": self.spec.strategy,
                "direction": self.spec.direction,
                "market_id": pair.market_id,
                "slug": pair.slug,
                "shares": self.spec.shares,
                "edge_target_per_share": self.spec.edge_target,
                "detected_edge_per_share": metrics["edge"],
                "detected_pair_price": metrics["pair_price"],
                "coverage_multiple": metrics["coverage"],
                "coverage_a": metrics["coverage_a"],
                "coverage_b": metrics["coverage_b"],
                "stability_ms": self.spec.stability_ms,
                "base_latency_ms": self.settings.dual_fok_base_latency_ms,
                "arrival_skew_ms": self.spec.arrival_skew_ms,
                "first_leg": first_leg,
                "limit_a": quote_a.marginal_price,
                "limit_b": quote_b.marginal_price,
                "detected_quote_a": _quote_payload(quote_a),
                "detected_quote_b": _quote_payload(quote_b),
                "prepositioned_complete_set_inventory": self.spec.direction == "SELL_PAIR",
            },
        )

    def process_due(self, engine: ArbitrageEngine) -> None:
        now = time.monotonic()
        for market_id in list(self.pending):
            pending = self.pending.get(market_id)
            if pending is None:
                continue
            a = engine.books.get(pending.pair.token_a)
            b = engine.books.get(pending.pair.token_b)
            if not a or not b:
                continue

            if not pending.processed_a and now >= pending.due_a:
                pending.fill_a = self._execute_leg(a, pending, "A")
                pending.processed_a = True
                pending.actual_arrival_a_ms = Decimal(str((now - pending.detected_at) * 1000))
                self._record_leg(pending, "A", pending.fill_a, pending.actual_arrival_a_ms)

            if not pending.processed_b and now >= pending.due_b:
                pending.fill_b = self._execute_leg(b, pending, "B")
                pending.processed_b = True
                pending.actual_arrival_b_ms = Decimal(str((now - pending.detected_at) * 1000))
                self._record_leg(pending, "B", pending.fill_b, pending.actual_arrival_b_ms)

            if not pending.processed_a or not pending.processed_b:
                continue
            if pending.fill_a and pending.fill_b:
                self.both_filled += 1
                self._finalize_both(pending, now)
                continue
            if not pending.fill_a and not pending.fill_b:
                self.neither_filled += 1
                self._finalize(pending, now, status="NEITHER_FILLED", action="NO_POSITION", pnl=ZERO, recovery=None)
                continue
            if pending.recovery_due is None:
                pending.recovery_started_at = now
                pending.recovery_due = now + self.settings.dual_fok_recovery_latency_ms / 1000
                continue
            if now >= pending.recovery_due:
                self._recover_one_leg(engine, pending, now)

    def _execute_leg(self, book, pending: PendingDualFOK, leg: str) -> ExecutionQuote | None:
        limit = pending.detected_limit_a if leg == "A" else pending.detected_limit_b
        if pending.spec.direction == "BUY_PAIR":
            return book.quote_buy(pending.spec.shares, max_price=limit)
        return book.quote_sell(pending.spec.shares, min_price=limit)

    def _record_leg(self, pending: PendingDualFOK, leg: str, fill: ExecutionQuote | None, arrival_ms: Decimal) -> None:
        self.recorder.write(
            "dual_fok_leg_result",
            {
                "strategy": self.spec.strategy,
                "direction": self.spec.direction,
                "market_id": pending.pair.market_id,
                "slug": pending.pair.slug,
                "leg": leg,
                "filled": fill is not None,
                "actual_arrival_ms": arrival_ms,
                "configured_base_latency_ms": self.settings.dual_fok_base_latency_ms,
                "configured_arrival_skew_ms": self.spec.arrival_skew_ms,
                "fill": _quote_payload(fill),
            },
        )

    def _finalize_both(self, pending: PendingDualFOK, now: float) -> None:
        assert pending.fill_a is not None and pending.fill_b is not None
        fee_a = taker_fee(pending.fill_a.segments, self.settings.crypto_taker_fee_rate)
        fee_b = taker_fee(pending.fill_b.segments, self.settings.crypto_taker_fee_rate)
        if pending.spec.direction == "BUY_PAIR":
            pnl = pending.spec.shares - pending.fill_a.notional - pending.fill_b.notional - fee_a - fee_b
            action = "MERGE_COMPLETE_SET"
        else:
            pnl = pending.fill_a.notional + pending.fill_b.notional - pending.spec.shares - fee_a - fee_b
            action = "SELL_PREPOSITIONED_COMPLETE_SET"
        self._finalize(pending, now, status="BOTH_FILLED", action=action, pnl=pnl, recovery=None)

    def _recover_one_leg(self, engine: ArbitrageEngine, pending: PendingDualFOK, now: float) -> None:
        a = engine.books[pending.pair.token_a]
        b = engine.books[pending.pair.token_b]
        shares = pending.spec.shares
        if pending.fill_a is not None:
            filled_leg, filled, filled_book, other_book = "A", pending.fill_a, a, b
        else:
            filled_leg, filled, filled_book, other_book = "B", pending.fill_b, b, a
        assert filled is not None
        fee_filled = taker_fee(filled.segments, self.settings.crypto_taker_fee_rate)

        completion_quote: ExecutionQuote | None = None
        unwind_quote: ExecutionQuote | None = None
        completion_pnl: Decimal | None = None
        unwind_pnl: Decimal | None = None

        if pending.spec.direction == "BUY_PAIR":
            completion_quote = other_book.quote_buy(shares)
            if completion_quote is not None:
                fee_completion = taker_fee(completion_quote.segments, self.settings.crypto_taker_fee_rate)
                completion_pnl = shares - filled.notional - completion_quote.notional - fee_filled - fee_completion
            unwind_quote = filled_book.quote_sell(shares)
            if unwind_quote is not None:
                fee_unwind = taker_fee(unwind_quote.segments, self.settings.crypto_taker_fee_rate)
                unwind_pnl = unwind_quote.notional - filled.notional - fee_filled - fee_unwind
            completion_action = "COMPLETE_MISSING_LEG"
            unwind_action = "UNWIND_FILLED_LEG"
        else:
            completion_quote = other_book.quote_sell(shares)
            if completion_quote is not None:
                fee_completion = taker_fee(completion_quote.segments, self.settings.crypto_taker_fee_rate)
                completion_pnl = filled.notional + completion_quote.notional - shares - fee_filled - fee_completion
            unwind_quote = filled_book.quote_buy(shares)
            if unwind_quote is not None:
                fee_unwind = taker_fee(unwind_quote.segments, self.settings.crypto_taker_fee_rate)
                unwind_pnl = filled.notional - unwind_quote.notional - fee_filled - fee_unwind
            completion_action = "SELL_REMAINING_TOKEN"
            unwind_action = "BUY_BACK_AND_MERGE"

        if completion_pnl is not None and (unwind_pnl is None or completion_pnl >= unwind_pnl):
            pnl = completion_pnl
            action = completion_action
            self.recovery_completions += 1
        elif unwind_pnl is not None:
            pnl = unwind_pnl
            action = unwind_action
            self.recovery_unwinds += 1
        else:
            if pending.spec.direction == "BUY_PAIR":
                pnl = -filled.notional - fee_filled
            else:
                pnl = filled.notional - shares - fee_filled
            action = "RECOVERY_LIQUIDITY_FAILURE"
            self.recovery_liquidity_failures += 1

        self.one_leg_miss += 1
        if shares > ZERO:
            self.miss_losses_per_share.append(max(-pnl, ZERO) / shares)
        recovery_started = pending.recovery_started_at or now
        recovery_payload = {
            "filled_leg": filled_leg,
            "configured_recovery_latency_ms": self.settings.dual_fok_recovery_latency_ms,
            "actual_recovery_latency_ms": Decimal(str(max(0.0, (now - recovery_started) * 1000))),
            "completion_quote": _quote_payload(completion_quote),
            "completion_pnl": completion_pnl,
            "unwind_quote": _quote_payload(unwind_quote),
            "unwind_pnl": unwind_pnl,
            "chosen_action": action,
        }
        self._finalize(pending, now, status="ONE_LEG_MISS", action=action, pnl=pnl, recovery=recovery_payload)

    def _finalize(
        self,
        pending: PendingDualFOK,
        now: float,
        *,
        status: str,
        action: str,
        pnl: Decimal,
        recovery: dict[str, Any] | None,
    ) -> None:
        self.pending.pop(pending.pair.market_id, None)
        self.cooldown_until[pending.pair.market_id] = now + self.settings.dual_fok_cooldown_ms / 1000
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
                "strategy": self.spec.strategy,
                "mode": "DUAL_FOK",
                "direction": self.spec.direction,
                "finalized_at": _utc_now(),
                "market_id": pending.pair.market_id,
                "slug": pending.pair.slug,
                "status": status,
                "action": action,
                "shares": self.spec.shares,
                "target_edge_per_share": self.spec.edge_target,
                "arrival_skew_ms": self.spec.arrival_skew_ms,
                "base_latency_ms": self.settings.dual_fok_base_latency_ms,
                "stability_ms": self.spec.stability_ms,
                "coverage_multiple": self.spec.coverage_multiple,
                "detected_edge_per_share": pending.detected_edge,
                "detected_pair_price": pending.detected_pair_price,
                "detected_coverage_multiple": pending.detected_coverage,
                "detected_coverage_a": pending.detected_coverage_a,
                "detected_coverage_b": pending.detected_coverage_b,
                "detected_limit_a": pending.detected_limit_a,
                "detected_limit_b": pending.detected_limit_b,
                "first_leg": pending.first_leg,
                "actual_arrival_a_ms": pending.actual_arrival_a_ms,
                "actual_arrival_b_ms": pending.actual_arrival_b_ms,
                "initial_execution": {"leg_a": _quote_payload(pending.fill_a), "leg_b": _quote_payload(pending.fill_b)},
                "leg_a_filled": pending.fill_a is not None,
                "leg_b_filled": pending.fill_b is not None,
                "recovery": recovery,
                "realized_pnl": pnl,
                "equity_after": equity_after,
                "prepositioned_complete_set_inventory": self.spec.direction == "SELL_PAIR",
            },
        )

    def diagnostic_row(self) -> dict[str, Any]:
        total_finalized = self.both_filled + self.one_leg_miss + self.neither_filled
        return {
            "strategy": self.spec.strategy,
            "direction": self.spec.direction,
            "shares": self.spec.shares,
            "edge_target": self.spec.edge_target,
            "arrival_skew_ms": self.spec.arrival_skew_ms,
            "coverage_multiple": self.spec.coverage_multiple,
            "stability_ms": self.spec.stability_ms,
            "base_latency_ms": self.settings.dual_fok_base_latency_ms,
            "pending": len(self.pending),
            "opportunities": self.opportunities_started,
            "lifetime_samples": self.opportunities_ended,
            "avg_lifetime_ms": _avg(self.opportunity_lifetimes_ms),
            "median_lifetime_ms": Decimal(str(median(self.opportunity_lifetimes_ms))) if self.opportunity_lifetimes_ms else ZERO,
            "placements": self.placements,
            "finalized": total_finalized,
            "both_filled": self.both_filled,
            "one_leg_miss": self.one_leg_miss,
            "neither_filled": self.neither_filled,
            "p_both": Decimal(self.both_filled) / Decimal(total_finalized) if total_finalized else ZERO,
            "p_miss": Decimal(self.one_leg_miss) / Decimal(total_finalized) if total_finalized else ZERO,
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
            "stability_waits": self.stability_waits,
            "book_rejects": self.book_rejects,
        }


class DualFOKResearchSuite:
    def __init__(self, settings: Settings, recorder: JsonlRecorder) -> None:
        self.settings = settings
        self.recorder = recorder
        self.variants: list[DualFOKVariantEngine] = []
        if settings.dual_fok_enabled:
            self.variants.extend(self._buy_variants())
        if settings.reverse_dual_fok_enabled:
            self.variants.extend(self._reverse_variants())

    @staticmethod
    def _edge_code(edge: Decimal) -> str:
        return f"{int(edge * Decimal('1000')):02d}"

    @staticmethod
    def _cov_code(cov: Decimal) -> str:
        return str(cov.normalize()).replace(".", "p")

    def _make(
        self,
        *,
        prefix: str,
        direction: str,
        shares: Decimal,
        edge: Decimal,
        skew: int,
        coverage: Decimal,
        stability: int,
        family: str,
    ) -> DualFOKVariantEngine:
        strategy = (
            f"{prefix}-{family}-S{shares.normalize()}-E{self._edge_code(edge)}"
            f"-SK{skew}-C{self._cov_code(coverage)}-ST{stability}"
        )
        return DualFOKVariantEngine(
            self.settings,
            self.recorder,
            DualFOKVariantSpec(
                strategy=strategy,
                direction=direction,
                shares=shares,
                edge_target=edge,
                arrival_skew_ms=skew,
                coverage_multiple=coverage,
                stability_ms=stability,
            ),
        )

    def _buy_variants(self) -> list[DualFOKVariantEngine]:
        s = self.settings
        out: list[DualFOKVariantEngine] = []
        for skew in s.dual_fok_skews_ms:
            out.append(self._make(prefix="DFOK", direction="BUY_PAIR", shares=s.dual_fok_primary_size, edge=s.dual_fok_primary_edge_target, skew=skew, coverage=s.dual_fok_primary_coverage_multiple, stability=s.dual_fok_primary_stability_ms, family="SK"))
        for shares in s.dual_fok_size_candidates:
            if shares != s.dual_fok_primary_size:
                out.append(self._make(prefix="DFOK", direction="BUY_PAIR", shares=shares, edge=s.dual_fok_primary_edge_target, skew=s.dual_fok_primary_skew_ms, coverage=s.dual_fok_primary_coverage_multiple, stability=s.dual_fok_primary_stability_ms, family="SZ"))
        for edge in s.dual_fok_edge_targets:
            if edge != s.dual_fok_primary_edge_target:
                out.append(self._make(prefix="DFOK", direction="BUY_PAIR", shares=s.dual_fok_primary_size, edge=edge, skew=s.dual_fok_primary_skew_ms, coverage=s.dual_fok_primary_coverage_multiple, stability=s.dual_fok_primary_stability_ms, family="ED"))
        for coverage in s.dual_fok_coverage_multiples:
            if coverage != s.dual_fok_primary_coverage_multiple:
                out.append(self._make(prefix="DFOK", direction="BUY_PAIR", shares=s.dual_fok_primary_size, edge=s.dual_fok_primary_edge_target, skew=s.dual_fok_primary_skew_ms, coverage=coverage, stability=s.dual_fok_primary_stability_ms, family="CV"))
        for stability in s.dual_fok_stability_periods_ms:
            if stability != s.dual_fok_primary_stability_ms:
                out.append(self._make(prefix="DFOK", direction="BUY_PAIR", shares=s.dual_fok_primary_size, edge=s.dual_fok_primary_edge_target, skew=s.dual_fok_primary_skew_ms, coverage=s.dual_fok_primary_coverage_multiple, stability=stability, family="ST"))
        return out

    def _reverse_variants(self) -> list[DualFOKVariantEngine]:
        s = self.settings
        return [
            self._make(prefix="RFOK", direction="SELL_PAIR", shares=s.dual_fok_primary_size, edge=s.dual_fok_primary_edge_target, skew=skew, coverage=s.dual_fok_primary_coverage_multiple, stability=s.dual_fok_primary_stability_ms, family="SK")
            for skew in s.reverse_dual_fok_skews_ms
        ]

    def on_market_update(self, engine: ArbitrageEngine, market_id: str, surge: SurgeSnapshot | None = None) -> None:
        for variant in self.variants:
            variant.on_market_update(engine, market_id, surge)

    def process_due(self, engine: ArbitrageEngine) -> None:
        for variant in self.variants:
            variant.process_due(engine)

    def diagnostic_rows(self) -> list[dict[str, Any]]:
        return [variant.diagnostic_row() for variant in self.variants]

    def ranked_rows(self) -> list[dict[str, Any]]:
        rows = [row for row in self.diagnostic_rows() if row["placements"] > 0]
        return sorted(rows, key=lambda row: (row["ev_per_placement"], row["p_both"], -row["p_miss"]), reverse=True)
