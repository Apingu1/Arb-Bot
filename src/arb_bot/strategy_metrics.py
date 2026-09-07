from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from .storage import JsonlRecorder


ZERO = Decimal("0")
ONE = Decimal("1")


@dataclass(slots=True)
class StrategyEquity:
    strategy: str
    recorder: JsonlRecorder
    equity: Decimal = ZERO
    realized_events: int = 0
    wins: int = 0
    losses: int = 0
    flats: int = 0
    peak_equity: Decimal = ZERO
    max_drawdown: Decimal = ZERO

    def apply(
        self,
        pnl: Decimal,
        *,
        market_id: str,
        slug: str,
        status: str,
        action: str,
    ) -> Decimal:
        self.equity += pnl
        self.realized_events += 1
        if pnl > ZERO:
            self.wins += 1
        elif pnl < ZERO:
            self.losses += 1
        else:
            self.flats += 1
        if self.equity > self.peak_equity:
            self.peak_equity = self.equity
        drawdown = self.peak_equity - self.equity
        if drawdown > self.max_drawdown:
            self.max_drawdown = drawdown

        self.recorder.write(
            "strategy_equity",
            {
                "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "strategy": self.strategy,
                "market_id": market_id,
                "slug": slug,
                "status": status,
                "action": action,
                "pnl_delta": pnl,
                "equity": self.equity,
                "realized_events": self.realized_events,
                "wins": self.wins,
                "losses": self.losses,
                "flats": self.flats,
                "max_drawdown": self.max_drawdown,
            },
        )
        return self.equity


@dataclass(slots=True)
class EmpiricalLegRisk:
    """Observed taker leg-miss risk.

    This is deliberately measurement-only for now. The taker strategy continues
    to use its configured fixed risk reserve until enough observations exist and
    empirical sizing is explicitly enabled in a later phase.
    """

    attempts: int = 0
    completed: int = 0
    leg_misses: int = 0
    neither_filled: int = 0
    cumulative_miss_loss: Decimal = ZERO
    cumulative_miss_loss_per_share: Decimal = ZERO

    def observe(self, *, status: str, pnl: Decimal, shares: Decimal) -> None:
        self.attempts += 1
        if status == "BOTH_FILLED":
            self.completed += 1
            return
        if status == "ONE_LEG_MISS":
            self.leg_misses += 1
            loss = max(-pnl, ZERO)
            self.cumulative_miss_loss += loss
            if shares > ZERO:
                self.cumulative_miss_loss_per_share += loss / shares
            return
        self.neither_filled += 1

    @property
    def miss_probability(self) -> Decimal:
        if self.attempts <= 0:
            return ZERO
        return Decimal(self.leg_misses) / Decimal(self.attempts)

    @property
    def average_miss_loss(self) -> Decimal:
        if self.leg_misses <= 0:
            return ZERO
        return self.cumulative_miss_loss / Decimal(self.leg_misses)

    @property
    def average_miss_loss_per_share(self) -> Decimal:
        if self.leg_misses <= 0:
            return ZERO
        return self.cumulative_miss_loss_per_share / Decimal(self.leg_misses)

    @property
    def estimated_reserve_per_share(self) -> Decimal:
        return self.miss_probability * self.average_miss_loss_per_share

    def ready(self, minimum_samples: int) -> bool:
        return self.attempts >= max(1, minimum_samples)
