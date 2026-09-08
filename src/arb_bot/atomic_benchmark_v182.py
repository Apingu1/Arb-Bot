from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Any

from .atomic_benchmark import (
    AtomicWindow,
    IdealAtomicBenchmarkSuite,
    IdealAtomicVariant,
    _quote_payload,
    _size_code,
    _utc_now,
)
from .discovery import asset_from_slug
from .storage import JsonlRecorder
from .strategy import ArbitrageEngine


log = logging.getLogger(__name__)
ATOMIC_LATENCY_CHECKPOINTS_MS = (1, 2, 5, 10, 25, 50, 100)


class IdealAtomicVariantV182(IdealAtomicVariant):
    """Ideal atomic control with observational latency-survival checkpoints.

    A checkpoint is recorded at the first market update observed at or after the
    target latency while the same complete-set snapshot remains fee-adjusted
    positive. ``actual_elapsed_ms`` is always stored so delayed observations are
    visible rather than silently treated as exact-latency executions.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._sampled_checkpoints: dict[str, set[int]] = {}
        self.latency_survival_counts = {
            checkpoint: 0 for checkpoint in ATOMIC_LATENCY_CHECKPOINTS_MS
        }

    def _sample_latency(
        self,
        market_id: str,
        now: float,
        metrics: dict[str, Any],
    ) -> None:
        window = self.active.get(market_id)
        if window is None:
            return
        elapsed_ms = Decimal(str(max(0.0, (now - window.started_at) * 1000)))
        sampled = self._sampled_checkpoints.setdefault(market_id, set())
        pair = metrics["pair"]
        asset = asset_from_slug(pair.slug) or "UNKNOWN"

        for checkpoint in ATOMIC_LATENCY_CHECKPOINTS_MS:
            if checkpoint in sampled or elapsed_ms < Decimal(checkpoint):
                continue
            sampled.add(checkpoint)
            self.latency_survival_counts[checkpoint] += 1
            self.recorder.write(
                "atomic_benchmark_latency_sample",
                {
                    "strategy": self.strategy,
                    "mode": "IDEAL_ATOMIC_BENCHMARK",
                    "direction": self.direction,
                    "benchmark_only": True,
                    "market_id": pair.market_id,
                    "slug": pair.slug,
                    "asset": asset,
                    "target_latency_ms": checkpoint,
                    "actual_elapsed_ms": elapsed_ms,
                    "sampled_at": _utc_now(),
                    "edge_per_share": metrics["edge"],
                    "pnl": metrics["pnl"],
                    "pair_price": metrics["pair_price"],
                    "quote_a": _quote_payload(metrics["quote_a"]),
                    "quote_b": _quote_payload(metrics["quote_b"]),
                    "taker_fee_paid": metrics["fee_a"] + metrics["fee_b"],
                },
            )

    def on_market_update(self, engine: ArbitrageEngine, market_id: str) -> None:
        now = time.monotonic()
        metrics = self._snapshot(engine, market_id, now)
        if metrics is None or metrics["edge"] < self.settings.atomic_min_net_edge_per_share:
            self._close(market_id, now, "NO_LONGER_POSITIVE")
            return

        existing = self.active.get(market_id)
        if existing is not None:
            existing.peak_edge = max(existing.peak_edge, metrics["edge"])
            self._sample_latency(market_id, now, metrics)
            return

        pair = metrics["pair"]
        window = AtomicWindow(
            slug=pair.slug,
            started_at=now,
            started_at_utc=_utc_now(),
            capture_edge=metrics["edge"],
            peak_edge=metrics["edge"],
            capture_pnl=metrics["pnl"],
            pair_price=metrics["pair_price"],
        )
        self.active[market_id] = window
        self._sampled_checkpoints[market_id] = set()
        self.captures += 1
        self.benchmark_pnl += metrics["pnl"]
        self.edges.append(metrics["edge"])
        asset = asset_from_slug(pair.slug) or "UNKNOWN"
        self.asset_captures[asset] = self.asset_captures.get(asset, 0) + 1
        self.asset_pnl[asset] = self.asset_pnl.get(asset, Decimal("0")) + metrics["pnl"]
        for band in self.edge_band_counts:
            if metrics["edge"] >= band:
                self.edge_band_counts[band] += 1

        self.recorder.write(
            "atomic_benchmark_capture",
            {
                "strategy": self.strategy,
                "mode": "IDEAL_ATOMIC_BENCHMARK",
                "direction": self.direction,
                "benchmark_only": True,
                "captured_at": window.started_at_utc,
                "finalized_at": window.started_at_utc,
                "market_id": pair.market_id,
                "slug": pair.slug,
                "asset": asset,
                "status": "CAPTURED",
                "action": "INSTANT_SIMULTANEOUS_COMPLETE_SET",
                "shares": self.shares,
                "detected_pair_price": metrics["pair_price"],
                "detected_edge_per_share": metrics["edge"],
                "taker_fee_paid": metrics["fee_a"] + metrics["fee_b"],
                "initial_execution": {
                    "leg_a": _quote_payload(metrics["quote_a"]),
                    "leg_b": _quote_payload(metrics["quote_b"]),
                },
                "realized_pnl": metrics["pnl"],
                "equity_after": self.benchmark_pnl,
                "prepositioned_complete_set_inventory": self.direction == "SELL_PAIR",
                "phase182_latency_instrumented": True,
            },
        )

    def _close(self, market_id: str, now: float, reason: str) -> None:
        super()._close(market_id, now, reason)
        self._sampled_checkpoints.pop(market_id, None)

    def diagnostic_row(self) -> dict[str, Any]:
        row = super().diagnostic_row()
        row["latency_survival_counts"] = dict(self.latency_survival_counts)
        return row


class IdealAtomicBenchmarkSuiteV182(IdealAtomicBenchmarkSuite):
    def __init__(self, settings, recorder: JsonlRecorder) -> None:
        self.settings = settings
        self.recorder = recorder
        self.variants: list[IdealAtomicVariantV182] = []
        if not settings.atomic_benchmark_enabled:
            return
        for shares in settings.atomic_sizes:
            self.variants.append(
                IdealAtomicVariantV182(
                    settings,
                    recorder,
                    shares=shares,
                    direction="BUY_PAIR",
                )
            )
            if settings.atomic_reverse_enabled:
                self.variants.append(
                    IdealAtomicVariantV182(
                        settings,
                        recorder,
                        shares=shares,
                        direction="SELL_PAIR",
                    )
                )


def log_atomic_diagnostics_v182(suite: IdealAtomicBenchmarkSuiteV182) -> None:
    for row in suite.diagnostic_rows():
        if not row["captures"] and not row["active_windows"]:
            continue
        survival = {
            f"{checkpoint}ms": f"{count}/{row['captures']}"
            for checkpoint, count in row.get("latency_survival_counts", {}).items()
            if count or checkpoint <= 10
        }
        log.info(
            "%s | IDEAL ONLY captures=%d active=%d pnl=%+.5f avg_edge=%+.5f "
            "life(avg/p50)=%.1f/%.1fms survival=%s bands=%s",
            row["strategy"],
            row["captures"],
            row["active_windows"],
            float(row["benchmark_pnl"]),
            float(row["avg_edge"]),
            float(row["avg_lifetime_ms"]),
            float(row["median_lifetime_ms"]),
            survival,
            row["edge_band_counts"],
        )
