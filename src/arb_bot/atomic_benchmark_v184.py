from __future__ import annotations

import logging
import time
from collections import defaultdict
from decimal import Decimal
from typing import Any

from .atomic_benchmark import _quote_payload, _utc_now
from .atomic_benchmark_v183 import IdealAtomicVariantV183
from .discovery import asset_from_slug
from .research_context_v183 import PHASE183_RUN_ID
from .research_context_v184 import PHASE184_RUN_ID
from .storage import JsonlRecorder


log = logging.getLogger(__name__)


class ExecutableAtomicVariantV184(IdealAtomicVariantV183):
    """Local-book executable proxy layered on top of the ideal benchmark.

    For every ideal capture, Phase 1.8.4 schedules independent end-to-end
    latency scenarios (default 2/5/10 ms). At each deadline it re-quotes the
    full requested size on both books, includes taker fees and rejects stale
    books. A positive result is still a local-book shadow fill, not proof that
    two exchange orders would fill atomically.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._v184_exec_due: dict[str, dict[int, float]] = {}
        self.v184_exec_stats: dict[int, dict[str, Any]] = {
            latency: {
                "attempts": 0,
                "fills": 0,
                "pnl": Decimal("0"),
                "outcomes": defaultdict(int),
            }
            for latency in self.settings.v184_atomic_execution_latencies_ms
        }

    def _v184_schedule(self, market_id: str) -> None:
        window = self.active.get(market_id)
        if window is None or market_id in self._v184_exec_due:
            return
        self._v184_exec_due[market_id] = {
            int(latency): window.started_at + int(latency) / 1000
            for latency in self.settings.v184_atomic_execution_latencies_ms
        }

    def _v184_record(
        self,
        *,
        market_id: str,
        slug: str,
        latency_ms: int,
        actual_elapsed_ms: Decimal,
        outcome: str,
        metrics: dict[str, Any] | None = None,
        book_age_a_ms: Decimal | None = None,
        book_age_b_ms: Decimal | None = None,
        reason: str | None = None,
    ) -> None:
        stats = self.v184_exec_stats.setdefault(
            latency_ms,
            {"attempts": 0, "fills": 0, "pnl": Decimal("0"), "outcomes": defaultdict(int)},
        )
        stats["attempts"] += 1
        stats["outcomes"][outcome] += 1
        if outcome == "EXECUTABLE_SHADOW_FILL" and metrics is not None:
            stats["fills"] += 1
            stats["pnl"] += metrics["pnl"]

        payload: dict[str, Any] = {
            "phase183_run_id": PHASE183_RUN_ID,
            "phase184_run_id": PHASE184_RUN_ID,
            "strategy": self.strategy,
            "mode": "ATOMIC_EXECUTION_PROXY_V184",
            "direction": self.direction,
            "benchmark_only": True,
            "execution_proxy": True,
            "market_id": market_id,
            "slug": slug,
            "asset": asset_from_slug(slug) or "UNKNOWN",
            "target_latency_ms": latency_ms,
            "actual_elapsed_ms": actual_elapsed_ms,
            "outcome": outcome,
            "book_age_a_ms": book_age_a_ms,
            "book_age_b_ms": book_age_b_ms,
            "reason": reason,
            "observed_at": _utc_now(),
        }
        if metrics is not None:
            payload.update(
                {
                    "edge_per_share": metrics["edge"],
                    "pnl": metrics["pnl"],
                    "pair_price": metrics["pair_price"],
                    "quote_a": _quote_payload(metrics["quote_a"]),
                    "quote_b": _quote_payload(metrics["quote_b"]),
                    "taker_fee_paid": metrics["fee_a"] + metrics["fee_b"],
                }
            )
        self.recorder.write("atomic_execution_proxy_v184", payload)

    def on_market_update(self, engine, market_id: str) -> None:
        was_active = market_id in self.active
        super().on_market_update(engine, market_id)
        if not was_active and market_id in self.active:
            self._v184_schedule(market_id)

    def _v184_process_execution_deadlines(self, engine, now: float) -> None:
        for market_id in list(self._v184_exec_due):
            due_map = self._v184_exec_due.get(market_id)
            if not due_map:
                self._v184_exec_due.pop(market_id, None)
                continue
            window = self.active.get(market_id)
            if window is None:
                continue
            pair = engine.pairs.get(market_id)
            if pair is None:
                continue

            for latency_ms, deadline in list(due_map.items()):
                if now < deadline:
                    continue
                actual_elapsed_ms = Decimal(str(max(0.0, (now - window.started_at) * 1000)))
                a = engine.books.get(pair.token_a)
                b = engine.books.get(pair.token_b)
                age_a = (
                    Decimal(str(max(0.0, (now - a.updated_monotonic) * 1000)))
                    if a is not None
                    else None
                )
                age_b = (
                    Decimal(str(max(0.0, (now - b.updated_monotonic) * 1000)))
                    if b is not None
                    else None
                )
                max_age = Decimal(self.settings.v184_atomic_max_book_age_ms)
                if (
                    a is None
                    or b is None
                    or age_a is None
                    or age_b is None
                    or age_a > max_age
                    or age_b > max_age
                ):
                    self._v184_record(
                        market_id=market_id,
                        slug=window.slug,
                        latency_ms=latency_ms,
                        actual_elapsed_ms=actual_elapsed_ms,
                        outcome="STALE_OR_MISSING_BOOK",
                        book_age_a_ms=age_a,
                        book_age_b_ms=age_b,
                    )
                    due_map.pop(latency_ms, None)
                    continue

                metrics = self._snapshot(engine, market_id, now)
                if metrics is None:
                    self._v184_record(
                        market_id=market_id,
                        slug=window.slug,
                        latency_ms=latency_ms,
                        actual_elapsed_ms=actual_elapsed_ms,
                        outcome="NO_FULL_SIZE_QUOTE",
                        book_age_a_ms=age_a,
                        book_age_b_ms=age_b,
                    )
                elif metrics["edge"] < self.settings.v184_atomic_min_execution_edge_per_share:
                    self._v184_record(
                        market_id=market_id,
                        slug=window.slug,
                        latency_ms=latency_ms,
                        actual_elapsed_ms=actual_elapsed_ms,
                        outcome="NO_LONGER_PROFITABLE",
                        metrics=metrics,
                        book_age_a_ms=age_a,
                        book_age_b_ms=age_b,
                    )
                else:
                    self._v184_record(
                        market_id=market_id,
                        slug=window.slug,
                        latency_ms=latency_ms,
                        actual_elapsed_ms=actual_elapsed_ms,
                        outcome="EXECUTABLE_SHADOW_FILL",
                        metrics=metrics,
                        book_age_a_ms=age_a,
                        book_age_b_ms=age_b,
                    )
                due_map.pop(latency_ms, None)

            if not due_map:
                self._v184_exec_due.pop(market_id, None)

    def process_due(self, engine) -> None:
        now = time.monotonic()
        self._v184_process_execution_deadlines(engine, now)
        super().process_due(engine)

    def _close(self, market_id: str, now: float, reason: str) -> None:
        window = self.active.get(market_id)
        pending = self._v184_exec_due.pop(market_id, {})
        if window is not None:
            elapsed_ms = Decimal(str(max(0.0, (now - window.started_at) * 1000)))
            for latency_ms in sorted(pending):
                self._v184_record(
                    market_id=market_id,
                    slug=window.slug,
                    latency_ms=latency_ms,
                    actual_elapsed_ms=elapsed_ms,
                    outcome="EXPIRED_BEFORE_EXECUTION",
                    reason=reason,
                )
        super()._close(market_id, now, reason)

    def diagnostic_row(self) -> dict[str, Any]:
        row = super().diagnostic_row()
        row["v184_atomic_execution_proxy"] = {
            latency: {
                "attempts": stats["attempts"],
                "fills": stats["fills"],
                "pnl": stats["pnl"],
                "outcomes": dict(stats["outcomes"]),
            }
            for latency, stats in self.v184_exec_stats.items()
        }
        return row


class AtomicExecutionBenchmarkSuiteV184:
    def __init__(self, settings, recorder: JsonlRecorder) -> None:
        self.settings = settings
        self.recorder = recorder
        self.variants: list[ExecutableAtomicVariantV184] = []
        if not settings.atomic_benchmark_enabled:
            return
        for shares in settings.atomic_sizes:
            self.variants.append(
                ExecutableAtomicVariantV184(
                    settings,
                    recorder,
                    shares=shares,
                    direction="BUY_PAIR",
                )
            )
            if settings.atomic_reverse_enabled:
                self.variants.append(
                    ExecutableAtomicVariantV184(
                        settings,
                        recorder,
                        shares=shares,
                        direction="SELL_PAIR",
                    )
                )

    def on_market_update(self, engine, market_id: str) -> None:
        for variant in self.variants:
            variant.on_market_update(engine, market_id)

    def process_due(self, engine) -> None:
        for variant in self.variants:
            variant.process_due(engine)

    def diagnostic_rows(self) -> list[dict[str, Any]]:
        return [variant.diagnostic_row() for variant in self.variants]

    def asset_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for variant in self.variants:
            rows.extend(variant.asset_rows())
        return rows


def log_atomic_diagnostics_v184(suite: AtomicExecutionBenchmarkSuiteV184) -> None:
    for row in suite.diagnostic_rows():
        if not row["captures"] and not row["active_windows"]:
            continue
        execution = {
            f"{latency}ms": (
                f"{stats['fills']}/{stats['attempts']} pnl={float(stats['pnl']):+.4f}"
            )
            for latency, stats in row.get("v184_atomic_execution_proxy", {}).items()
            if stats["attempts"]
        }
        log.info(
            "%s | IDEAL captures=%d active=%d benchmark_pnl=%+.5f | V184 executable-proxy=%s",
            row["strategy"],
            row["captures"],
            row["active_windows"],
            float(row["benchmark_pnl"]),
            execution or "no attempts",
        )
