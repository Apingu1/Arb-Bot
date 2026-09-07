from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal

from .config import Settings
from .fees import taker_fee
from .models import Opportunity, ShadowResult
from .orderbook import TokenBook
from .storage import JsonlRecorder


log = logging.getLogger(__name__)
ZERO = Decimal("0")


@dataclass(slots=True)
class PendingShadowOrder:
    opportunity: Opportunity
    execute_at: float


class ShadowExecutor:
    def __init__(self, settings: Settings, recorder: JsonlRecorder) -> None:
        self.settings = settings
        self.recorder = recorder
        self.pending: dict[str, PendingShadowOrder] = {}
        self.cooldown_until: dict[str, float] = {}
        self.total_pnl = ZERO
        self.completed = 0
        self.leg_misses = 0
        self.rejected = 0

    def can_submit(self, market_id: str) -> bool:
        now = time.monotonic()
        return market_id not in self.pending and now >= self.cooldown_until.get(market_id, 0.0)

    def submit(self, opportunity: Opportunity) -> bool:
        if not self.can_submit(opportunity.market_id):
            return False
        self.pending[opportunity.market_id] = PendingShadowOrder(opportunity, time.monotonic() + self.settings.shadow_latency_ms / 1000)
        self.recorder.write("opportunity", opportunity)
        log.info("SHADOW intent %s | %.4f shares | expected +%.4f pUSD | edge %.4f/share", opportunity.slug, float(opportunity.shares), float(opportunity.expected_net_profit), float(opportunity.expected_net_edge_per_share))
        return True

    def process_due(self, books: dict[str, TokenBook]) -> list[ShadowResult]:
        now = time.monotonic()
        due = [market_id for market_id, pending in self.pending.items() if pending.execute_at <= now]
        results: list[ShadowResult] = []
        for market_id in due:
            pending = self.pending.pop(market_id)
            result = self._execute(pending.opportunity, books)
            self.cooldown_until[market_id] = now + self.settings.market_cooldown_ms / 1000
            self.total_pnl += result.pnl_usdc
            if result.status == "BOTH_FILLED": self.completed += 1
            elif result.status == "ONE_LEG_MISS": self.leg_misses += 1
            else: self.rejected += 1
            self.recorder.write("shadow_result", result)
            log.info("SHADOW %s %s | pnl=%+.4f pUSD | cumulative=%+.4f", result.status, result.slug, float(result.pnl_usdc), float(self.total_pnl))
            results.append(result)
        return results

    def _execute(self, opp: Opportunity, books: dict[str, TokenBook]) -> ShadowResult:
        a = books[opp.token_a]
        b = books[opp.token_b]
        fill_a = a.quote_buy(opp.shares, max_price=opp.detected_max_price_a)
        fill_b = b.quote_buy(opp.shares, max_price=opp.detected_max_price_b)
        if fill_a and fill_b:
            fees = taker_fee(fill_a.segments, self.settings.crypto_taker_fee_rate) + taker_fee(fill_b.segments, self.settings.crypto_taker_fee_rate)
            pnl = opp.shares - fill_a.notional - fill_b.notional - fees
            return ShadowResult(opp.market_id, opp.slug, "BOTH_FILLED", opp.shares, pnl, "MERGE_COMPLETE_SET", True, True, "Both simulated FOK legs filled inside their detection-time price limits.")
        if not fill_a and not fill_b:
            return ShadowResult(opp.market_id, opp.slug, "NEITHER_FILLED", opp.shares, ZERO, "NO_POSITION", False, False, "Neither simulated FOK leg remained executable after latency.")

        penalty = opp.shares * self.settings.recovery_penalty_per_share
        if fill_a:
            pnl, action, detail = self._recover(filled=fill_a, filled_book=a, missing_book=b, shares=opp.shares, filled_name=opp.outcome_a, missing_name=opp.outcome_b)
            return ShadowResult(opp.market_id, opp.slug, "ONE_LEG_MISS", opp.shares, pnl - penalty, action, True, False, detail)
        assert fill_b is not None
        pnl, action, detail = self._recover(filled=fill_b, filled_book=b, missing_book=a, shares=opp.shares, filled_name=opp.outcome_b, missing_name=opp.outcome_a)
        return ShadowResult(opp.market_id, opp.slug, "ONE_LEG_MISS", opp.shares, pnl - penalty, action, False, True, detail)

    def _recover(self, *, filled, filled_book: TokenBook, missing_book: TokenBook, shares: Decimal, filled_name: str, missing_name: str) -> tuple[Decimal, str, str]:
        fee_filled = taker_fee(filled.segments, self.settings.crypto_taker_fee_rate)
        completion = missing_book.quote_buy(shares)
        completion_pnl = None
        if completion:
            completion_fee = taker_fee(completion.segments, self.settings.crypto_taker_fee_rate)
            completion_pnl = shares - filled.notional - completion.notional - fee_filled - completion_fee
        unwind = filled_book.quote_sell(shares)
        unwind_pnl = None
        if unwind:
            unwind_fee = taker_fee(unwind.segments, self.settings.crypto_taker_fee_rate)
            unwind_pnl = unwind.notional - filled.notional - fee_filled - unwind_fee
        if completion_pnl is not None and (unwind_pnl is None or completion_pnl >= unwind_pnl):
            return completion_pnl, "COMPLETE_MISSING_LEG", f"{filled_name} filled; {missing_name} missed. Completing the pair was better than unwinding."
        if unwind_pnl is not None:
            return unwind_pnl, "UNWIND_FILLED_LEG", f"{filled_name} filled; {missing_name} missed. Unwinding the filled leg was the lower-loss route."
        return -filled.notional, "RECOVERY_LIQUIDITY_FAILURE", f"{filled_name} filled; no modeled completion or unwind liquidity was available."
