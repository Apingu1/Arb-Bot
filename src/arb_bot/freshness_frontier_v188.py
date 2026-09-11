from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from decimal import Decimal
from statistics import median
from typing import Any

from .batch_fok_v187 import (
    BatchFOKEngineV187,
    BatchRiskBookV187,
    PendingBatchFOKV187,
    SharedBatchSnapshotV187,
)
from .discovery import asset_from_slug
from .fees import taker_fee
from .profit_fok_v185 import ZERO
from .research_context_v188 import PHASE188_RUN_ID


class FreshnessBatchFOKEngineV188(BatchFOKEngineV187):
    """One controlled BFOK variant differing only by max book age."""

    mode = "BATCH_FOK_FRESHNESS_V188"

    def __init__(
        self,
        settings,
        recorder,
        *,
        max_book_age_ms: int,
        fixed_size: Decimal,
        risk_book: BatchRiskBookV187,
    ) -> None:
        tuned = replace(settings, v187_max_book_age_ms=int(max_book_age_ms))
        super().__init__(
            tuned,
            recorder,
            strategy=f"BFOK-FRESH{int(max_book_age_ms)}",
            fixed_size=fixed_size,
            risk_book=risk_book,
            ev_gate=False,
            contributes_risk=False,
        )
        self.max_book_age_ms = int(max_book_age_ms)
        self.freshness_blocks = 0
        self._freshness_context: dict[str, dict[str, Any]] = {}

    def _common(self) -> dict[str, Any]:
        payload = super()._common()
        payload.update(
            {
                "phase188_run_id": PHASE188_RUN_ID,
                "freshness_frontier": True,
                "max_book_age_ms": self.max_book_age_ms,
            }
        )
        return payload

    @staticmethod
    def _book_ages(engine, pair, at_monotonic: float) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
        book_a = engine.books.get(pair.token_a)
        book_b = engine.books.get(pair.token_b)
        if (
            book_a is None
            or book_b is None
            or book_a.updated_monotonic <= 0
            or book_b.updated_monotonic <= 0
        ):
            return None, None, None
        age_a = Decimal(str(max(0.0, (at_monotonic - book_a.updated_monotonic) * 1000)))
        age_b = Decimal(str(max(0.0, (at_monotonic - book_b.updated_monotonic) * 1000)))
        return age_a, age_b, abs(age_a - age_b)

    def _arrival_market_metric(self, engine, pending: PendingBatchFOKV187) -> dict[str, Any] | None:
        book_a = engine.books.get(pending.pair.token_a)
        book_b = engine.books.get(pending.pair.token_b)
        if book_a is None or book_b is None or not book_a.ready or not book_b.ready:
            return None
        qa = book_a.quote_buy(pending.shares)
        qb = book_b.quote_buy(pending.shares)
        if qa is None or qb is None:
            return None
        fee_a = taker_fee(qa.segments, self.settings.crypto_taker_fee_rate)
        fee_b = taker_fee(qb.segments, self.settings.crypto_taker_fee_rate)
        pnl = pending.shares - qa.notional - qb.notional - fee_a - fee_b
        return {
            "pnl": pnl,
            "edge": pnl / pending.shares if pending.shares > ZERO else ZERO,
            "pair_price": (qa.notional + qb.notional) / pending.shares if pending.shares > ZERO else ZERO,
        }

    def on_market_update_from_snapshot_with_ages(
        self,
        snapshot: SharedBatchSnapshotV187,
        surge,
        *,
        book_age_a_ms: Decimal | None,
        book_age_b_ms: Decimal | None,
        book_age_skew_ms: Decimal | None,
    ) -> None:
        if book_age_a_ms is None or book_age_b_ms is None:
            self.freshness_blocks += 1
            return
        limit = Decimal(self.max_book_age_ms)
        if book_age_a_ms > limit or book_age_b_ms > limit:
            self.freshness_blocks += 1
            return

        before = self.placements
        super().on_market_update_from_snapshot(snapshot, surge)
        if self.placements <= before:
            return

        metrics = snapshot.metrics.get(self.fixed_size) if self.fixed_size is not None else None
        if metrics is None:
            return
        context = {
            "detected_book_age_a_ms": book_age_a_ms,
            "detected_book_age_b_ms": book_age_b_ms,
            "detected_older_book_age_ms": max(book_age_a_ms, book_age_b_ms),
            "detected_book_age_skew_ms": book_age_skew_ms,
            "detected_edge_per_share": metrics["edge"],
            "detected_pnl": metrics["pnl"],
            "detected_coverage": metrics["coverage"],
        }
        self._freshness_context[snapshot.market_id] = context
        self.recorder.write(
            "freshness_candidate_v188",
            {
                **self._common(),
                "market_id": snapshot.market_id,
                "slug": snapshot.pair.slug,
                "asset": asset_from_slug(snapshot.pair.slug) or "UNKNOWN",
                "shares": self.fixed_size,
                **context,
            },
        )

    def _process_arrival(self, engine, pending: PendingBatchFOKV187, now: float) -> None:
        age_a, age_b, skew = self._book_ages(engine, pending.pair, now)
        market_metric = self._arrival_market_metric(engine, pending)
        ctx = self._freshness_context.setdefault(pending.pair.market_id, {})
        ctx.update(
            {
                "arrival_book_age_a_ms": age_a,
                "arrival_book_age_b_ms": age_b,
                "arrival_older_book_age_ms": (
                    max(age_a, age_b) if age_a is not None and age_b is not None else None
                ),
                "arrival_book_age_skew_ms": skew,
                "arrival_market_edge_per_share": (
                    market_metric["edge"] if market_metric is not None else None
                ),
                "arrival_market_pnl": market_metric["pnl"] if market_metric is not None else None,
                "arrival_market_pair_price": (
                    market_metric["pair_price"] if market_metric is not None else None
                ),
            }
        )
        super()._process_arrival(engine, pending, now)

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
        ctx = dict(self._freshness_context.get(pending.pair.market_id, {}))
        lifetime_ms = Decimal(str(max(0.0, (now - pending.detected_at) * 1000)))
        super()._finalize(
            pending,
            now,
            status=status,
            action=action,
            pnl=pnl,
            recovery=recovery,
        )
        self.recorder.write(
            "freshness_execution_v188",
            {
                **self._common(),
                "market_id": pending.pair.market_id,
                "slug": pending.pair.slug,
                "asset": asset_from_slug(pending.pair.slug) or "UNKNOWN",
                "shares": pending.shares,
                "status": status,
                "action": action,
                "realized_pnl": pnl,
                "realized_edge_per_share": pnl / pending.shares if pending.shares > ZERO else ZERO,
                "lifetime_ms": lifetime_ms,
                **ctx,
            },
        )
        self._freshness_context.pop(pending.pair.market_id, None)

    def diagnostic_row(self) -> dict[str, Any]:
        row = dict(super().diagnostic_row())
        row.update(
            {
                "max_book_age_ms": self.max_book_age_ms,
                "freshness_blocks": self.freshness_blocks,
            }
        )
        return row


class FreshnessFrontierSuiteV188:
    """25/35/50/75/100 ms BFOK frontier sharing one detection quote."""

    def __init__(self, settings, recorder) -> None:
        self.settings = settings
        self.recorder = recorder
        self.risk_book = BatchRiskBookV187(settings)
        self.fixed_size = settings.v188_freshness_size
        ages = sorted({int(v) for v in settings.v188_freshness_ages_ms if int(v) > 0})
        self.variants: list[FreshnessBatchFOKEngineV188] = []
        if settings.v188_freshness_frontier_enabled:
            for max_age in ages:
                self.variants.append(
                    FreshnessBatchFOKEngineV188(
                        settings,
                        recorder,
                        max_book_age_ms=max_age,
                        fixed_size=self.fixed_size,
                        risk_book=self.risk_book,
                    )
                )
        self._timer_handle: asyncio.TimerHandle | None = None
        self._timer_deadline: float | None = None
        self._timer_engine = None

    @staticmethod
    def _depth_at_limit(levels: dict[Decimal, Decimal], limit: Decimal) -> Decimal:
        return sum((qty for px, qty in levels.items() if px <= limit), ZERO)

    def _metric_for_size(self, engine, snapshot: SharedBatchSnapshotV187) -> dict[str, Any] | None:
        existing = snapshot.metrics.get(self.fixed_size)
        if existing is not None:
            return existing
        pair = snapshot.pair
        book_a = engine.books.get(pair.token_a)
        book_b = engine.books.get(pair.token_b)
        if book_a is None or book_b is None or not book_a.ready or not book_b.ready:
            return None
        qa = book_a.quote_buy(self.fixed_size)
        qb = book_b.quote_buy(self.fixed_size)
        if qa is None or qb is None:
            return None
        fee_a = taker_fee(qa.segments, self.settings.crypto_taker_fee_rate)
        fee_b = taker_fee(qb.segments, self.settings.crypto_taker_fee_rate)
        pnl = self.fixed_size - qa.notional - qb.notional - fee_a - fee_b
        cov_a = self._depth_at_limit(book_a.asks, qa.marginal_price) / self.fixed_size
        cov_b = self._depth_at_limit(book_b.asks, qb.marginal_price) / self.fixed_size
        return {
            "quote_a": qa,
            "quote_b": qb,
            "fee_a": fee_a,
            "fee_b": fee_b,
            "pnl": pnl,
            "edge": pnl / self.fixed_size,
            "pair_price": (qa.notional + qb.notional) / self.fixed_size,
            "coverage_a": cov_a,
            "coverage_b": cov_b,
            "coverage": min(cov_a, cov_b),
        }

    @staticmethod
    def _ages_at_snapshot(engine, snapshot: SharedBatchSnapshotV187) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
        book_a = engine.books.get(snapshot.pair.token_a)
        book_b = engine.books.get(snapshot.pair.token_b)
        if (
            book_a is None
            or book_b is None
            or book_a.updated_monotonic <= 0
            or book_b.updated_monotonic <= 0
        ):
            return None, None, None
        age_a = Decimal(str(max(0.0, (snapshot.started_at - book_a.updated_monotonic) * 1000)))
        age_b = Decimal(str(max(0.0, (snapshot.started_at - book_b.updated_monotonic) * 1000)))
        return age_a, age_b, abs(age_a - age_b)

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

    def on_market_update(
        self,
        engine,
        market_id: str,
        surge,
        source_snapshot: SharedBatchSnapshotV187 | None,
    ) -> None:
        if not self.variants or source_snapshot is None or source_snapshot.market_id != market_id:
            return
        metric = self._metric_for_size(engine, source_snapshot)
        metrics = {self.fixed_size: metric} if metric is not None else {}
        snapshot = SharedBatchSnapshotV187(
            market_id=source_snapshot.market_id,
            pair=source_snapshot.pair,
            started_at=source_snapshot.started_at,
            completed_at=source_snapshot.completed_at,
            metrics=metrics,
        )
        age_a, age_b, skew = self._ages_at_snapshot(engine, source_snapshot)
        for variant in self.variants:
            variant.on_market_update_from_snapshot_with_ages(
                snapshot,
                surge,
                book_age_a_ms=age_a,
                book_age_b_ms=age_b,
                book_age_skew_ms=skew,
            )
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
