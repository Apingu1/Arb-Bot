from __future__ import annotations

import time
from collections import Counter
from decimal import Decimal
from typing import Any

from .discovery import MarketPhase, asset_from_slug, market_phase
from .fees import taker_fee
from .research_context_v189 import PHASE189_RUN_ID


ZERO = Decimal("0")


class RawOpportunityObserverV189:
    """Minimal observation-only replacement for the hyper-active BFOK-RAW.

    Every ready two-sided market update is still inspected at the configured RAW
    size, but no pending order, FOK lifecycle, recovery, StrategyEquity event or
    per-loss JSON row is created. Non-positive observations are only accumulated
    in memory and emitted as compact periodic rollups. Positive fee-adjusted
    observations receive one detailed event with book-age attribution.
    """

    mode = "RAW_OBSERVER_V189"
    strategy = "RAW-OBS"

    def __init__(self, settings, recorder) -> None:
        self.settings = settings
        self.recorder = recorder
        self.enabled = bool(settings.v189_raw_observer_enabled)
        self.shares = settings.v189_latency_size
        self.rollup_seconds = max(1, int(settings.v189_raw_observer_rollup_seconds))
        self.observations = 0
        self.full_pairs = 0
        self.positive = 0
        self.nonpositive = 0
        self.positive_pnl_upper_bound = ZERO
        self._counts: Counter[str] = Counter()
        self._rollup_positive_pnl = ZERO
        self._last_rollup = time.monotonic()

    @staticmethod
    def _book_ages(engine, pair, now: float) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
        a = engine.books.get(pair.token_a)
        b = engine.books.get(pair.token_b)
        if (
            a is None
            or b is None
            or a.updated_monotonic <= 0
            or b.updated_monotonic <= 0
        ):
            return None, None, None
        age_a = Decimal(str(max(0.0, (now - a.updated_monotonic) * 1000)))
        age_b = Decimal(str(max(0.0, (now - b.updated_monotonic) * 1000)))
        return age_a, age_b, abs(age_a - age_b)

    def _maybe_rollup(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_rollup < self.rollup_seconds:
            return
        if self._counts:
            self.recorder.write(
                "raw_observer_rollup_v189",
                {
                    "phase189_run_id": PHASE189_RUN_ID,
                    "strategy": self.strategy,
                    "mode": self.mode,
                    "window_seconds": self.rollup_seconds,
                    "counts": dict(self._counts),
                    "positive_pnl_upper_bound": self._rollup_positive_pnl,
                    "note": "Observation-only; no orders or execution lifecycle are modeled.",
                },
            )
        self._counts.clear()
        self._rollup_positive_pnl = ZERO
        self._last_rollup = now

    def on_market_update(self, engine, market_id: str) -> None:
        if not self.enabled:
            return
        self.observations += 1
        self._counts["MARKET_UPDATES"] += 1

        pair = engine.pairs.get(market_id)
        if pair is None or market_phase(pair) != MarketPhase.LIVE:
            self._counts["NOT_LIVE"] += 1
            self._maybe_rollup()
            return
        a = engine.books.get(pair.token_a)
        b = engine.books.get(pair.token_b)
        if a is None or b is None or not a.ready or not b.ready:
            self._counts["BOOK_NOT_READY"] += 1
            self._maybe_rollup()
            return

        qa = a.quote_buy(self.shares)
        qb = b.quote_buy(self.shares)
        if qa is None or qb is None:
            self._counts["NO_FULL_PAIR"] += 1
            self._maybe_rollup()
            return

        self.full_pairs += 1
        self._counts["FULL_PAIR"] += 1
        fee_a = taker_fee(qa.segments, self.settings.crypto_taker_fee_rate)
        fee_b = taker_fee(qb.segments, self.settings.crypto_taker_fee_rate)
        pnl = self.shares - qa.notional - qb.notional - fee_a - fee_b
        edge = pnl / self.shares if self.shares > ZERO else ZERO
        now = time.monotonic()
        age_a, age_b, skew = self._book_ages(engine, pair, now)

        if edge >= Decimal("0.005"):
            self._counts["EDGE_GE_005"] += 1
        elif edge >= Decimal("0.003"):
            self._counts["EDGE_GE_003"] += 1
        elif edge >= Decimal("0.001"):
            self._counts["EDGE_GE_001"] += 1
        elif edge > ZERO:
            self._counts["EDGE_GT_0"] += 1

        if pnl > ZERO:
            self.positive += 1
            self.positive_pnl_upper_bound += pnl
            self._rollup_positive_pnl += pnl
            self._counts["POSITIVE"] += 1
            self.recorder.write(
                "raw_positive_observation_v189",
                {
                    "phase189_run_id": PHASE189_RUN_ID,
                    "strategy": self.strategy,
                    "mode": self.mode,
                    "market_id": market_id,
                    "slug": pair.slug,
                    "asset": asset_from_slug(pair.slug) or "UNKNOWN",
                    "shares": self.shares,
                    "fee_adjusted_edge_per_share": edge,
                    "pnl_upper_bound": pnl,
                    "pair_price": (qa.notional + qb.notional) / self.shares if self.shares > ZERO else ZERO,
                    "book_age_a_ms": age_a,
                    "book_age_b_ms": age_b,
                    "older_book_age_ms": max(age_a, age_b) if age_a is not None and age_b is not None else None,
                    "book_age_skew_ms": skew,
                    "observation_only": True,
                    "execution_latency_ms": None,
                    "note": "Positive local-book observation only; not a fill or deployable P&L claim.",
                },
            )
        else:
            self.nonpositive += 1
            self._counts["NONPOSITIVE"] += 1

        self._maybe_rollup()

    def process_due(self, engine=None) -> None:
        self._maybe_rollup()

    def force_rollup(self) -> None:
        self._maybe_rollup(force=True)

    def diagnostic_row(self) -> dict[str, Any]:
        # The dashboard row deliberately exposes only positive observations as
        # upper-bound candidates. It does not present non-positive observations
        # as synthetic trading losses and does not contribute StrategyEquity.
        count = self.positive
        return {
            "strategy": self.strategy,
            "mode": self.mode,
            "direction": "OBSERVE_ONLY",
            "pending": 0,
            "placements": count,
            "completed": count,
            "both_filled": count,
            "one_leg_miss": 0,
            "neither_filled": 0,
            "misses": 0,
            "p_both": Decimal("1") if count else ZERO,
            "p_miss": ZERO,
            "ev_per_placement": (
                self.positive_pnl_upper_bound / Decimal(count) if count else ZERO
            ),
            "equity": ZERO,
            "avg_lifetime_ms": ZERO,
            "median_lifetime_ms": ZERO,
            "candidates": count,
            "opportunities": self.full_pairs,
            "raw_positive_observations": self.positive,
            "raw_nonpositive_observations": self.nonpositive,
            "raw_positive_pnl_upper_bound": self.positive_pnl_upper_bound,
            "observation_only": True,
        }
