from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from .config import Settings
from .fees import taker_fee
from .models import ExecutionQuote, Opportunity, ShadowResult
from .orderbook import TokenBook
from .storage import JsonlRecorder
from .strategy_metrics import EmpiricalLegRisk, StrategyEquity


log = logging.getLogger(__name__)
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
        "segments": [
            {"price": segment.price, "shares": segment.shares}
            for segment in quote.segments
        ],
    }


@dataclass(slots=True)
class PendingShadowOrder:
    opportunity: Opportunity
    submitted_at: float
    submitted_at_utc: str
    execute_at: float


@dataclass(slots=True)
class PendingRecovery:
    opportunity: Opportunity
    filled_leg: str
    fill_a: ExecutionQuote | None
    fill_b: ExecutionQuote | None
    execution_at: float
    execution_at_utc: str
    actual_execution_latency_ms: Decimal
    recover_at: float


class ShadowExecutor:
    """Two-leg taker shadow executor with explicit execution and recovery latency."""

    strategy_name = "TAKER"

    def __init__(self, settings: Settings, recorder: JsonlRecorder) -> None:
        self.settings = settings
        self.recorder = recorder
        self.pending: dict[str, PendingShadowOrder] = {}
        self.recoveries: dict[str, PendingRecovery] = {}
        self.cooldown_until: dict[str, float] = {}
        self.total_pnl = ZERO
        self.completed = 0
        self.leg_misses = 0
        self.rejected = 0
        self.last_summary: dict[str, Any] | None = None
        self.equity = StrategyEquity(self.strategy_name, recorder)
        self.empirical_risk = EmpiricalLegRisk()

    @property
    def pending_count(self) -> int:
        return len(self.pending) + len(self.recoveries)

    def can_submit(self, market_id: str) -> bool:
        now = time.monotonic()
        return (
            market_id not in self.pending
            and market_id not in self.recoveries
            and now >= self.cooldown_until.get(market_id, 0.0)
        )

    def submit(self, opportunity: Opportunity) -> bool:
        if not self.can_submit(opportunity.market_id):
            return False
        now = time.monotonic()
        submitted_utc = _utc_now()
        self.pending[opportunity.market_id] = PendingShadowOrder(
            opportunity=opportunity,
            submitted_at=now,
            submitted_at_utc=submitted_utc,
            execute_at=now + self.settings.shadow_latency_ms / 1000,
        )
        self.recorder.write("opportunity", opportunity)
        self.recorder.write(
            "taker_opportunity",
            {
                "strategy": self.strategy_name,
                "submitted_at": submitted_utc,
                "market_id": opportunity.market_id,
                "slug": opportunity.slug,
                "shares": opportunity.shares,
                "detected_at": opportunity.detected_at_utc,
                "detected_best_ask_a": opportunity.detected_best_ask_a,
                "detected_best_ask_b": opportunity.detected_best_ask_b,
                "detected_cost_a": opportunity.detected_cost_a,
                "detected_cost_b": opportunity.detected_cost_b,
                "detected_marginal_a": opportunity.detected_max_price_a,
                "detected_marginal_b": opportunity.detected_max_price_b,
                "detected_pair_vwap": (opportunity.detected_cost_a + opportunity.detected_cost_b) / opportunity.shares,
                "gross_profit": opportunity.gross_profit,
                "taker_fees": opportunity.taker_fees,
                "risk_reserve": opportunity.risk_reserve,
                "expected_net_profit": opportunity.expected_net_profit,
                "expected_net_edge_per_share": opportunity.expected_net_edge_per_share,
                "configured_execution_latency_ms": self.settings.shadow_latency_ms,
                "configured_recovery_latency_ms": self.settings.shadow_recovery_latency_ms,
            },
        )
        log.info(
            "TAKER intent %s | %.4f shares | expected +%.4f pUSD | edge %.4f/share",
            opportunity.slug,
            float(opportunity.shares),
            float(opportunity.expected_net_profit),
            float(opportunity.expected_net_edge_per_share),
        )
        return True

    def process_due(self, books: dict[str, TokenBook]) -> list[ShadowResult]:
        now = time.monotonic()
        results: list[ShadowResult] = []

        due_orders = [market_id for market_id, pending in self.pending.items() if pending.execute_at <= now]
        for market_id in due_orders:
            pending = self.pending.pop(market_id)
            result = self._execute_initial(pending, books, now)
            if result is not None:
                results.append(self._finalize(result, pending.opportunity, now, initial=pending))

        now = time.monotonic()
        due_recoveries = [market_id for market_id, pending in self.recoveries.items() if pending.recover_at <= now]
        for market_id in due_recoveries:
            recovery = self.recoveries.pop(market_id)
            result, recovery_payload = self._finish_recovery(recovery, books, now)
            results.append(
                self._finalize(
                    result,
                    recovery.opportunity,
                    now,
                    recovery=recovery,
                    recovery_payload=recovery_payload,
                )
            )

        return results

    def _execute_initial(
        self,
        pending: PendingShadowOrder,
        books: dict[str, TokenBook],
        now: float,
    ) -> ShadowResult | None:
        opp = pending.opportunity
        a = books[opp.token_a]
        b = books[opp.token_b]
        fill_a = a.quote_buy(opp.shares, max_price=opp.detected_max_price_a)
        fill_b = b.quote_buy(opp.shares, max_price=opp.detected_max_price_b)
        actual_latency_ms = Decimal(str((now - opp.detected_monotonic) * 1000))
        execution_at_utc = _utc_now()

        self.recorder.write(
            "taker_execution_stage",
            {
                "strategy": self.strategy_name,
                "stage": "INITIAL_EXECUTION",
                "market_id": opp.market_id,
                "slug": opp.slug,
                "shares": opp.shares,
                "execution_at": execution_at_utc,
                "configured_latency_ms": self.settings.shadow_latency_ms,
                "actual_latency_ms": actual_latency_ms,
                "leg_a": _quote_payload(fill_a),
                "leg_b": _quote_payload(fill_b),
            },
        )

        if fill_a and fill_b:
            fees = taker_fee(fill_a.segments, self.settings.crypto_taker_fee_rate) + taker_fee(
                fill_b.segments, self.settings.crypto_taker_fee_rate
            )
            pnl = opp.shares - fill_a.notional - fill_b.notional - fees
            result = ShadowResult(
                opp.market_id,
                opp.slug,
                "BOTH_FILLED",
                opp.shares,
                pnl,
                "MERGE_COMPLETE_SET",
                True,
                True,
                "Both simulated FOK legs filled inside their detection-time price limits.",
            )
            # Store the exact fills temporarily on the pending object via a summary event.
            self.recorder.write(
                "taker_fill_detail",
                {
                    "market_id": opp.market_id,
                    "slug": opp.slug,
                    "leg_a": _quote_payload(fill_a),
                    "leg_b": _quote_payload(fill_b),
                    "fees": fees,
                    "actual_execution_latency_ms": actual_latency_ms,
                },
            )
            return result

        if not fill_a and not fill_b:
            return ShadowResult(
                opp.market_id,
                opp.slug,
                "NEITHER_FILLED",
                opp.shares,
                ZERO,
                "NO_POSITION",
                False,
                False,
                "Neither simulated FOK leg remained executable after execution latency.",
            )

        filled_leg = "A" if fill_a else "B"
        self.recoveries[opp.market_id] = PendingRecovery(
            opportunity=opp,
            filled_leg=filled_leg,
            fill_a=fill_a,
            fill_b=fill_b,
            execution_at=now,
            execution_at_utc=execution_at_utc,
            actual_execution_latency_ms=actual_latency_ms,
            recover_at=now + self.settings.shadow_recovery_latency_ms / 1000,
        )
        self.recorder.write(
            "taker_leg_miss_detected",
            {
                "strategy": self.strategy_name,
                "market_id": opp.market_id,
                "slug": opp.slug,
                "shares": opp.shares,
                "filled_leg": filled_leg,
                "execution_at": execution_at_utc,
                "actual_execution_latency_ms": actual_latency_ms,
                "leg_a": _quote_payload(fill_a),
                "leg_b": _quote_payload(fill_b),
                "configured_recovery_latency_ms": self.settings.shadow_recovery_latency_ms,
            },
        )
        log.info(
            "TAKER leg miss %s | leg=%s filled | recovery scheduled in %dms",
            opp.slug,
            filled_leg,
            self.settings.shadow_recovery_latency_ms,
        )
        return None

    def _finish_recovery(
        self,
        recovery: PendingRecovery,
        books: dict[str, TokenBook],
        now: float,
    ) -> tuple[ShadowResult, dict[str, Any]]:
        opp = recovery.opportunity
        a = books[opp.token_a]
        b = books[opp.token_b]
        actual_recovery_latency_ms = Decimal(str((now - recovery.execution_at) * 1000))
        penalty = opp.shares * self.settings.recovery_penalty_per_share

        if recovery.filled_leg == "A":
            filled = recovery.fill_a
            assert filled is not None
            filled_book = a
            missing_book = b
            missing_name = opp.outcome_b
            filled_name = opp.outcome_a
        else:
            filled = recovery.fill_b
            assert filled is not None
            filled_book = b
            missing_book = a
            missing_name = opp.outcome_a
            filled_name = opp.outcome_b

        fee_filled = taker_fee(filled.segments, self.settings.crypto_taker_fee_rate)
        completion = missing_book.quote_buy(opp.shares)
        completion_fee = ZERO
        completion_pnl: Decimal | None = None
        if completion:
            completion_fee = taker_fee(completion.segments, self.settings.crypto_taker_fee_rate)
            completion_pnl = opp.shares - filled.notional - completion.notional - fee_filled - completion_fee

        unwind = filled_book.quote_sell(opp.shares)
        unwind_fee = ZERO
        unwind_pnl: Decimal | None = None
        if unwind:
            unwind_fee = taker_fee(unwind.segments, self.settings.crypto_taker_fee_rate)
            unwind_pnl = unwind.notional - filled.notional - fee_filled - unwind_fee

        if completion_pnl is not None and (unwind_pnl is None or completion_pnl >= unwind_pnl):
            pre_penalty_pnl = completion_pnl
            action = "COMPLETE_MISSING_LEG"
            details = f"{filled_name} filled; {missing_name} missed. Delayed completion was better than unwinding."
        elif unwind_pnl is not None:
            pre_penalty_pnl = unwind_pnl
            action = "UNWIND_FILLED_LEG"
            details = f"{filled_name} filled; {missing_name} missed. Delayed unwind was the lower-loss route."
        else:
            pre_penalty_pnl = -filled.notional - fee_filled
            action = "RECOVERY_LIQUIDITY_FAILURE"
            details = f"{filled_name} filled; no completion or unwind liquidity was available after recovery latency."

        pnl = pre_penalty_pnl - penalty
        result = ShadowResult(
            opp.market_id,
            opp.slug,
            "ONE_LEG_MISS",
            opp.shares,
            pnl,
            action,
            recovery.fill_a is not None,
            recovery.fill_b is not None,
            details,
        )
        payload = {
            "recovery_at": _utc_now(),
            "configured_recovery_latency_ms": self.settings.shadow_recovery_latency_ms,
            "actual_recovery_latency_ms": actual_recovery_latency_ms,
            "filled_leg": recovery.filled_leg,
            "initial_fill_a": _quote_payload(recovery.fill_a),
            "initial_fill_b": _quote_payload(recovery.fill_b),
            "completion_quote": _quote_payload(completion),
            "completion_fee": completion_fee,
            "completion_pnl": completion_pnl,
            "unwind_quote": _quote_payload(unwind),
            "unwind_fee": unwind_fee,
            "unwind_pnl": unwind_pnl,
            "chosen_action": action,
            "pre_penalty_pnl": pre_penalty_pnl,
            "residual_recovery_penalty": penalty,
            "final_pnl": pnl,
        }
        self.recorder.write("taker_recovery_stage", {"strategy": self.strategy_name, "market_id": opp.market_id, "slug": opp.slug, **payload})
        return result, payload

    def _finalize(
        self,
        result: ShadowResult,
        opp: Opportunity,
        now: float,
        *,
        initial: PendingShadowOrder | None = None,
        recovery: PendingRecovery | None = None,
        recovery_payload: dict[str, Any] | None = None,
    ) -> ShadowResult:
        self.cooldown_until[opp.market_id] = now + self.settings.market_cooldown_ms / 1000
        self.total_pnl += result.pnl_usdc
        if result.status == "BOTH_FILLED":
            self.completed += 1
        elif result.status == "ONE_LEG_MISS":
            self.leg_misses += 1
        else:
            self.rejected += 1

        self.empirical_risk.observe(status=result.status, pnl=result.pnl_usdc, shares=result.shares)
        equity_after = self.equity.apply(
            result.pnl_usdc,
            market_id=result.market_id,
            slug=result.slug,
            status=result.status,
            action=result.action,
        )
        self.recorder.write("shadow_result", result)

        actual_execution_latency_ms: Decimal | None = None
        if recovery is not None:
            actual_execution_latency_ms = recovery.actual_execution_latency_ms
        elif initial is not None:
            actual_execution_latency_ms = Decimal(str((now - opp.detected_monotonic) * 1000))

        summary = {
            "strategy": self.strategy_name,
            "finalized_at": _utc_now(),
            "market_id": opp.market_id,
            "slug": opp.slug,
            "status": result.status,
            "action": result.action,
            "shares": opp.shares,
            "detected_at": opp.detected_at_utc,
            "detected_best_ask_a": opp.detected_best_ask_a,
            "detected_best_ask_b": opp.detected_best_ask_b,
            "detected_cost_a": opp.detected_cost_a,
            "detected_cost_b": opp.detected_cost_b,
            "detected_average_a": opp.detected_cost_a / opp.shares,
            "detected_average_b": opp.detected_cost_b / opp.shares,
            "detected_marginal_a": opp.detected_max_price_a,
            "detected_marginal_b": opp.detected_max_price_b,
            "detected_pair_vwap": (opp.detected_cost_a + opp.detected_cost_b) / opp.shares,
            "detected_gross_profit": opp.gross_profit,
            "detected_taker_fees": opp.taker_fees,
            "detected_risk_reserve": opp.risk_reserve,
            "detected_expected_net_profit": opp.expected_net_profit,
            "actual_execution_latency_ms": actual_execution_latency_ms,
            "configured_execution_latency_ms": self.settings.shadow_latency_ms,
            "configured_recovery_latency_ms": self.settings.shadow_recovery_latency_ms,
            "leg_a_filled": result.leg_a_filled,
            "leg_b_filled": result.leg_b_filled,
            "recovery": recovery_payload,
            "realized_pnl": result.pnl_usdc,
            "equity_after": equity_after,
            "empirical_attempts": self.empirical_risk.attempts,
            "empirical_leg_misses": self.empirical_risk.leg_misses,
            "empirical_miss_probability": self.empirical_risk.miss_probability,
            "empirical_avg_miss_loss": self.empirical_risk.average_miss_loss,
            "empirical_avg_miss_loss_per_share": self.empirical_risk.average_miss_loss_per_share,
            "empirical_reserve_per_share": self.empirical_risk.estimated_reserve_per_share,
            "details": result.details,
        }
        self.last_summary = summary
        self.recorder.write("taker_execution_summary", summary)
        log.info(
            "TAKER %s %s | action=%s pnl=%+.4f pUSD | equity=%+.4f",
            result.status,
            result.slug,
            result.action,
            float(result.pnl_usdc),
            float(self.total_pnl),
        )
        return result
