from __future__ import annotations

import logging
import time
from collections import defaultdict
from decimal import Decimal
from typing import Any

from .atomic_benchmark import _utc_now
from .atomic_benchmark_v182 import (
    ATOMIC_LATENCY_CHECKPOINTS_MS,
    IdealAtomicVariantV182,
)
from .discovery import asset_from_slug
from .research_context_v183 import PHASE183_RUN_ID
from .storage import JsonlRecorder


log = logging.getLogger(__name__)


class IdealAtomicVariantV183(IdealAtomicVariantV182):
    """Phase 1.8.2 atomic replay with explicit missed-checkpoint reasons."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.latency_outcome_counts: dict[int, dict[str, int]] = {
            checkpoint: defaultdict(int) for checkpoint in ATOMIC_LATENCY_CHECKPOINTS_MS
        }

    def _emit_latency_outcome(
        self,
        *,
        market_id: str,
        slug: str,
        checkpoint: int,
        outcome: str,
        actual_elapsed_ms: Decimal,
        edge_per_share: Decimal | None = None,
        pnl: Decimal | None = None,
        close_reason: str | None = None,
    ) -> None:
        self.latency_outcome_counts[checkpoint][outcome] += 1
        self.recorder.write(
            "atomic_benchmark_latency_outcome_v183",
            {
                "phase183_run_id": PHASE183_RUN_ID,
                "strategy": self.strategy,
                "mode": "IDEAL_ATOMIC_BENCHMARK",
                "direction": self.direction,
                "benchmark_only": True,
                "market_id": market_id,
                "slug": slug,
                "asset": asset_from_slug(slug) or "UNKNOWN",
                "target_latency_ms": checkpoint,
                "outcome": outcome,
                "actual_elapsed_ms": actual_elapsed_ms,
                "edge_per_share": edge_per_share,
                "pnl": pnl,
                "close_reason": close_reason,
                "observed_at": _utc_now(),
            },
        )

    def _sample_latency(self, market_id: str, now: float, metrics: dict[str, Any]) -> None:
        before = set(self._sampled_checkpoints.get(market_id, set()))
        super()._sample_latency(market_id, now, metrics)
        after = set(self._sampled_checkpoints.get(market_id, set()))
        newly_sampled = sorted(after - before)
        if not newly_sampled:
            return
        window = self.active.get(market_id)
        if window is None:
            return
        elapsed_ms = Decimal(str(max(0.0, (now - window.started_at) * 1000)))
        pair = metrics["pair"]
        for checkpoint in newly_sampled:
            self._emit_latency_outcome(
                market_id=pair.market_id,
                slug=pair.slug,
                checkpoint=checkpoint,
                outcome="SURVIVED",
                actual_elapsed_ms=elapsed_ms,
                edge_per_share=metrics["edge"],
                pnl=metrics["pnl"],
            )

    def on_market_update(self, engine, market_id: str) -> None:
        was_active = market_id in self.active
        before_captures = self.captures
        super().on_market_update(engine, market_id)
        if was_active or market_id not in self.active or self.captures <= before_captures:
            return
        window = self.active[market_id]
        self.recorder.write(
            "atomic_benchmark_capture_v183",
            {
                "phase183_run_id": PHASE183_RUN_ID,
                "strategy": self.strategy,
                "direction": self.direction,
                "benchmark_only": True,
                "market_id": market_id,
                "slug": window.slug,
                "asset": asset_from_slug(window.slug) or "UNKNOWN",
                "captured_at": window.started_at_utc,
                "capture_edge": window.capture_edge,
                "capture_pnl": window.capture_pnl,
                "pair_price": window.pair_price,
            },
        )

    def _close(self, market_id: str, now: float, reason: str) -> None:
        window = self.active.get(market_id)
        if window is not None:
            elapsed_ms = Decimal(str(max(0.0, (now - window.started_at) * 1000)))
            sampled = set(self._sampled_checkpoints.get(market_id, set()))
            for checkpoint in ATOMIC_LATENCY_CHECKPOINTS_MS:
                if checkpoint in sampled:
                    continue
                outcome = (
                    "EXPIRED_BEFORE_CHECKPOINT"
                    if elapsed_ms < Decimal(checkpoint)
                    else "SCHEDULER_MISSED_CHECKPOINT"
                )
                self._emit_latency_outcome(
                    market_id=market_id,
                    slug=window.slug,
                    checkpoint=checkpoint,
                    outcome=outcome,
                    actual_elapsed_ms=elapsed_ms,
                    close_reason=reason,
                )
            self.recorder.write(
                "atomic_benchmark_lifetime_v183",
                {
                    "phase183_run_id": PHASE183_RUN_ID,
                    "strategy": self.strategy,
                    "direction": self.direction,
                    "benchmark_only": True,
                    "market_id": market_id,
                    "slug": window.slug,
                    "asset": asset_from_slug(window.slug) or "UNKNOWN",
                    "lifetime_ms": elapsed_ms,
                    "close_reason": reason,
                    "capture_edge": window.capture_edge,
                    "peak_edge": window.peak_edge,
                    "capture_pnl": window.capture_pnl,
                    "closed_at": _utc_now(),
                },
            )
        super()._close(market_id, now, reason)

    def diagnostic_row(self) -> dict[str, Any]:
        row = super().diagnostic_row()
        row["latency_outcome_counts_v183"] = {
            checkpoint: dict(counts)
            for checkpoint, counts in self.latency_outcome_counts.items()
        }
        return row


class IdealAtomicBenchmarkSuiteV183:
    def __init__(self, settings, recorder: JsonlRecorder) -> None:
        self.settings = settings
        self.recorder = recorder
        self.variants: list[IdealAtomicVariantV183] = []
        if not settings.atomic_benchmark_enabled:
            return
        for shares in settings.atomic_sizes:
            self.variants.append(
                IdealAtomicVariantV183(
                    settings,
                    recorder,
                    shares=shares,
                    direction="BUY_PAIR",
                )
            )
            if settings.atomic_reverse_enabled:
                self.variants.append(
                    IdealAtomicVariantV183(
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


def log_atomic_diagnostics_v183(suite: IdealAtomicBenchmarkSuiteV183) -> None:
    for row in suite.diagnostic_rows():
        if not row["captures"] and not row["active_windows"]:
            continue
        compact = {}
        for checkpoint in (1, 2, 5, 10):
            counts = row.get("latency_outcome_counts_v183", {}).get(checkpoint, {})
            survived = counts.get("SURVIVED", 0)
            expired = counts.get("EXPIRED_BEFORE_CHECKPOINT", 0)
            missed = counts.get("SCHEDULER_MISSED_CHECKPOINT", 0)
            if survived or expired or missed:
                compact[f"{checkpoint}ms"] = f"S{survived}/E{expired}/M{missed}"
        log.info(
            "%s | IDEAL ONLY captures=%d active=%d pnl=%+.5f avg_edge=%+.5f "
            "life(avg/p50)=%.1f/%.1fms checkpoint_outcomes=%s bands=%s",
            row["strategy"],
            row["captures"],
            row["active_windows"],
            float(row["benchmark_pnl"]),
            float(row["avg_edge"]),
            float(row["avg_lifetime_ms"]),
            float(row["median_lifetime_ms"]),
            compact,
            row["edge_band_counts"],
        )
