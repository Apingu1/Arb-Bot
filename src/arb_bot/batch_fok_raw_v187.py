from __future__ import annotations

import time
from dataclasses import replace
from decimal import Decimal
from typing import Any

from .batch_fok_v187 import (
    BatchFOKEngineV187,
    PreciseBatchFOKSuiteV187,
    SharedBatchSnapshotV187,
)
from .discovery import MarketPhase, market_phase
from .fees import taker_fee
from .profit_fok_v185 import ZERO


class RawBatchFOKEngineV187(BatchFOKEngineV187):
    """Deliberately ungated zero-latency BFOK diagnostic.

    This is an upper-bound research probe, not a live-execution model. It has
    no minimum edge, coverage-multiple, surge, book-age, EV or cooldown gate,
    and zero modeled batch-arrival/recovery latency. Structural requirements
    remain: ready books and enough displayed depth for both FOK legs.
    """

    mode = "BATCH_FOK_RAW_V187"

    def _book_fresh(self, book, now: float) -> bool:
        return book is not None and book.ready

    def _common(self) -> dict[str, Any]:
        payload = super()._common()
        payload.update(
            {
                "raw_ungated": True,
                "zero_latency_upper_bound": True,
                "disabled_strategy_gates": [
                    "MIN_EDGE",
                    "COVERAGE_MULTIPLE",
                    "SURGE",
                    "BOOK_AGE",
                    "EV",
                    "COOLDOWN",
                ],
            }
        )
        return payload


class BatchFOKWithRawSuiteV187:
    """Standard Phase 1.8.7 BFOK frontier plus BFOK-RAW.

    RAW is evaluated first. The protected BFOK snapshot is then built exactly
    once and shared both by all protected BFOK variants and the opportunity
    funnel, avoiding duplicate full-book quoting on the latency-sensitive path.
    """

    def __init__(self, settings, recorder) -> None:
        self.settings = settings
        self.recorder = recorder
        self.base = PreciseBatchFOKSuiteV187(settings, recorder)
        self.risk_book = self.base.risk_book
        self.last_raw_snapshot: SharedBatchSnapshotV187 | None = None
        self.last_standard_snapshot: SharedBatchSnapshotV187 | None = None

        self.raw: RawBatchFOKEngineV187 | None = None
        if getattr(settings, "v187_raw_enabled", True):
            raw_size = Decimal(str(getattr(settings, "v187_raw_size", Decimal("1"))))
            raw_settings = replace(
                settings,
                v187_detection_min_edge_per_share=Decimal("-10"),
                v187_detection_coverage_multiple=ZERO,
                v187_max_book_age_ms=2_147_483_647,
                v187_batch_arrival_latency_ms=0,
                v187_recovery_latency_ms=0,
                v187_cooldown_ms=0,
                v187_use_surge_gate=False,
                v187_ev_enabled=False,
            )
            self.raw = RawBatchFOKEngineV187(
                raw_settings,
                recorder,
                strategy="BFOK-RAW",
                fixed_size=raw_size,
                risk_book=self.risk_book,
                ev_gate=False,
                contributes_risk=False,
            )

        self.variants = [*self.base.variants]
        if self.raw is not None:
            self.variants.append(self.raw)

    @staticmethod
    def _depth_at_limit(levels: dict[Decimal, Decimal], limit: Decimal) -> Decimal:
        return sum((qty for px, qty in levels.items() if px <= limit), ZERO)

    def _build_raw_snapshot(self, engine, market_id: str) -> SharedBatchSnapshotV187 | None:
        raw = self.raw
        if raw is None:
            return None
        pair = engine.pairs.get(market_id)
        if pair is None or market_phase(pair) != MarketPhase.LIVE:
            return None

        started = time.monotonic()
        book_a = engine.books.get(pair.token_a)
        book_b = engine.books.get(pair.token_b)
        metrics: dict[Decimal, dict[str, Any]] = {}
        if book_a is None or book_b is None or not book_a.ready or not book_b.ready:
            return SharedBatchSnapshotV187(market_id, pair, started, time.monotonic(), metrics)

        shares = raw.fixed_size
        assert shares is not None
        qa = book_a.quote_buy(shares)
        qb = book_b.quote_buy(shares)
        if qa is not None and qb is not None:
            fee_a = taker_fee(qa.segments, raw.settings.crypto_taker_fee_rate)
            fee_b = taker_fee(qb.segments, raw.settings.crypto_taker_fee_rate)
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

    def on_market_update(self, engine, market_id: str, surge=None) -> None:
        # RAW first, processed immediately at its explicit zero-latency upper
        # bound so later protected research cannot delay this diagnostic.
        self.last_raw_snapshot = None
        self.last_standard_snapshot = None
        if self.raw is not None:
            self.last_raw_snapshot = self._build_raw_snapshot(engine, market_id)
            if self.last_raw_snapshot is not None:
                self.raw.on_market_update_from_snapshot(self.last_raw_snapshot, surge=None)
                self.raw.process_due(engine)
                self.raw.process_due(engine)

        # Build the protected snapshot once. This reproduces
        # PreciseBatchFOKSuiteV187.on_market_update without rebuilding the same
        # quotes a second time for funnel diagnostics.
        self.last_standard_snapshot = self.base._build_snapshot(engine, market_id)
        if self.last_standard_snapshot is not None:
            for variant in self.base.variants:
                variant.on_market_update_from_snapshot(self.last_standard_snapshot, surge)
            self.base._arm_timer(engine)

    def process_due(self, engine) -> None:
        if self.raw is not None:
            self.raw.process_due(engine)
        self.base.process_due(engine)

    def diagnostic_rows(self) -> list[dict[str, Any]]:
        return [variant.diagnostic_row() for variant in self.variants]

    def ranked_rows(self) -> list[dict[str, Any]]:
        rows = [row for row in self.diagnostic_rows() if row["placements"] > 0]
        return sorted(
            rows,
            key=lambda row: (row["ev_per_placement"], row["p_both"], -row["p_miss"]),
            reverse=True,
        )
