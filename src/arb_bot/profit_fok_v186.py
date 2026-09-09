from __future__ import annotations

import time
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from .discovery import MarketPhase, asset_from_slug, market_phase
from .fees import taker_fee
from .profit_fok_v185 import PendingProfitFOK, ProfitFOKEngineV185, ZERO, _utc_now
from .research_context_v183 import PHASE183_RUN_ID
from .research_context_v184 import PHASE184_RUN_ID
from .research_context_v185 import PHASE185_RUN_ID
from .research_context_v186 import PHASE186_RUN_ID
from .winner_research_v184 import WinnerResearchSuiteV184
from .profit_fok_v185_diag import DiagnosedProfitFOKSuiteV185


@dataclass(slots=True)
class SharedPFOKSnapshotV186:
    market_id: str
    started_at: float
    completed_at: float
    metrics: dict[Decimal, dict[str, Any]]


class InstrumentedProfitFOKEngineV186(ProfitFOKEngineV185):
    """Prioritized PFOK engine using one shared detection snapshot per update."""

    def __init__(self, settings, recorder, strategy: str) -> None:
        self.strategy = strategy
        self._shared_snapshot: SharedPFOKSnapshotV186 | None = None
        super().__init__(settings, recorder)

    def _common(self) -> dict[str, Any]:
        return {
            "phase183_run_id": PHASE183_RUN_ID,
            "phase184_run_id": PHASE184_RUN_ID,
            "phase185_run_id": PHASE185_RUN_ID,
            "phase186_run_id": PHASE186_RUN_ID,
            "strategy": self.strategy,
            "mode": "PROFIT_FOK_V186_FAST",
            "direction": self.direction,
        }

    def _candidate(self, engine, pair, now):
        snapshot = self._shared_snapshot
        if snapshot is None or snapshot.market_id != pair.market_id:
            return super()._candidate(engine, pair, now)

        saw_quote = False
        saw_edge = False
        for shares in sorted(self.settings.v185_profit_sizes, reverse=True):
            metrics = snapshot.metrics.get(shares)
            if metrics is None:
                continue
            saw_quote = True
            if metrics["edge"] < self.settings.v185_detection_min_edge_per_share:
                continue
            saw_edge = True
            if metrics["coverage"] < self.settings.v185_detection_coverage_multiple:
                continue
            return shares, metrics
        if saw_quote and not saw_edge:
            self.edge_rejects += 1
        elif saw_edge:
            self.coverage_rejects += 1
        return None

    def on_market_update_from_snapshot(self, engine, market_id: str, snapshot, surge=None) -> None:
        before = self.candidates
        self._shared_snapshot = snapshot
        try:
            super().on_market_update(engine, market_id, surge)
        finally:
            self._shared_snapshot = None

        if self.candidates > before:
            pending = self.pending.get(market_id)
            if pending is not None:
                timing = getattr(engine, "v186_event_timing", {}).get(market_id, {})
                callback_started = timing.get("callback_started")
                book_updated = timing.get("book_updated")
                payload = {
                    **self._common(),
                    "market_id": market_id,
                    "slug": pending.pair.slug,
                    "asset": asset_from_slug(pending.pair.slug) or "UNKNOWN",
                    "stage": "CANDIDATE",
                    "shares": pending.shares,
                    "detected_edge_per_share": pending.detected_edge,
                    "configured_preflight_latency_ms": self.settings.v185_base_latency_ms,
                    "snapshot_build_ms": Decimal(str(max(0.0, (snapshot.completed_at - snapshot.started_at) * 1000))),
                    "callback_to_snapshot_ms": (
                        Decimal(str(max(0.0, (snapshot.started_at - callback_started) * 1000)))
                        if callback_started is not None
                        else None
                    ),
                    "book_update_ms": (
                        Decimal(str(max(0.0, (book_updated - callback_started) * 1000)))
                        if callback_started is not None and book_updated is not None
                        else None
                    ),
                    "callback_to_candidate_ms": (
                        Decimal(str(max(0.0, (pending.detected_at - callback_started) * 1000)))
                        if callback_started is not None
                        else None
                    ),
                }
                self.recorder.write("pfok_latency_v186", payload)

    def _record_preflight_latency(self, pending, now: float, outcome: str) -> None:
        self.recorder.write(
            "pfok_latency_v186",
            {
                **self._common(),
                "market_id": pending.pair.market_id,
                "slug": pending.pair.slug,
                "asset": asset_from_slug(pending.pair.slug) or "UNKNOWN",
                "stage": "PREFLIGHT",
                "outcome": outcome,
                "shares": pending.shares,
                "configured_preflight_latency_ms": self.settings.v185_base_latency_ms,
                "actual_preflight_latency_ms": Decimal(str(max(0.0, (now - pending.detected_at) * 1000))),
                "scheduler_slippage_ms": Decimal(str(max(0.0, (now - pending.preflight_due) * 1000))),
                "detected_edge_per_share": pending.detected_edge,
                "preflight_edge_per_share": pending.preflight_edge,
            },
        )

    def _process_preflight(self, engine, pending: PendingProfitFOK, now: float) -> None:
        placements_before = self.placements
        rejects_before = self.preflight_rejects
        super()._process_preflight(engine, pending, now)
        if self.placements > placements_before:
            self._record_preflight_latency(pending, now, "PLACED")
        elif self.preflight_rejects > rejects_before:
            self._record_preflight_latency(pending, now, "REJECTED")

    def _process_second_leg(self, engine, pending: PendingProfitFOK, now: float) -> None:
        due = pending.second_due
        super()._process_second_leg(engine, pending, now)
        self.recorder.write(
            "pfok_latency_v186",
            {
                **self._common(),
                "market_id": pending.pair.market_id,
                "slug": pending.pair.slug,
                "asset": asset_from_slug(pending.pair.slug) or "UNKNOWN",
                "stage": "SECOND_LEG",
                "shares": pending.shares,
                "actual_second_arrival_ms": pending.second_arrival_ms,
                "scheduler_slippage_ms": (
                    Decimal(str(max(0.0, (now - due) * 1000))) if due is not None else None
                ),
                "filled": pending.second_fill is not None,
            },
        )


class RequoteProfitFOKEngineV186(InstrumentedProfitFOKEngineV186):
    """Fast PFOK whose delayed preflight re-quotes the *current* complete set.

    Detection economics remain identical to control PFOK. At preflight the two
    books are re-quoted at current prices, fees/depth are recomputed, and the
    trade is allowed only when the current combined edge still exceeds the
    existing preflight floor. The new marginal prices become the FOK limits for
    the separated first/second-leg simulation.
    """

    def _process_preflight(self, engine, pending: PendingProfitFOK, now: float) -> None:
        metrics = self._pair_metrics(
            engine,
            pending.pair,
            pending.shares,
            now,
            limit_a=None,
            limit_b=None,
        )
        if metrics is None:
            self._abort_preflight(pending, now, "REQUOTE_NO_FULL_SIZE_OR_STALE_BOOK")
            self._record_preflight_latency(pending, now, "REJECTED")
            return
        if metrics["edge"] < self.settings.v185_preflight_min_edge_per_share:
            pending.preflight_edge = metrics["edge"]
            self._abort_preflight(pending, now, "REQUOTE_EDGE_DECAYED_BEFORE_FIRST_LEG", metrics)
            self._record_preflight_latency(pending, now, "REJECTED")
            return
        if metrics["coverage"] < self.settings.v185_preflight_coverage_multiple:
            pending.preflight_edge = metrics["edge"]
            self._abort_preflight(pending, now, "REQUOTE_DEPTH_DECAYED_BEFORE_FIRST_LEG", metrics)
            self._record_preflight_latency(pending, now, "REJECTED")
            return

        pending.preflight_edge = metrics["edge"]
        pending.limit_a = metrics["quote_a"].marginal_price
        pending.limit_b = metrics["quote_b"].marginal_price
        pending.first_leg = "A" if metrics["coverage_a"] <= metrics["coverage_b"] else "B"
        pending.first_fill = metrics["quote_a"] if pending.first_leg == "A" else metrics["quote_b"]
        pending.first_arrival_ms = Decimal(str(max(0.0, (now - pending.detected_at) * 1000)))
        pending.second_due = now + self.settings.v185_leg_gap_ms / 1000
        self.placements += 1
        self.size_counts[pending.shares] = self.size_counts.get(pending.shares, 0) + 1

        self.recorder.write(
            "dual_fok_attempt_placed",
            {
                **self._common(),
                "market_id": pending.pair.market_id,
                "slug": pending.pair.slug,
                "shares": pending.shares,
                "target_edge_per_share": self.settings.v185_final_min_edge_per_share,
                "detected_edge_per_share": pending.detected_edge,
                "detected_pair_price": pending.detected_pair_price,
                "detected_coverage_multiple": min(pending.detected_coverage_a, pending.detected_coverage_b),
                "coverage_multiple": self.settings.v185_detection_coverage_multiple,
                "stability_ms": 0,
                "base_latency_ms": self.settings.v185_base_latency_ms,
                "arrival_skew_ms": self.settings.v185_leg_gap_ms,
                "first_leg": pending.first_leg,
                "limit_a": pending.limit_a,
                "limit_b": pending.limit_b,
                "preflight_edge_per_share": metrics["edge"],
                "actual_first_arrival_ms": pending.first_arrival_ms,
                "requote_preflight": True,
            },
        )
        self._record_leg(pending, pending.first_leg, pending.first_fill, pending.first_arrival_ms)
        self._record_preflight_latency(pending, now, "PLACED")


class FastPFOKSuiteV186:
    """Six latency-frontier variants evaluated from one shared snapshot."""

    def __init__(self, settings, recorder) -> None:
        self.settings = settings
        self.recorder = recorder
        self.variants: list[InstrumentedProfitFOKEngineV186] = []
        if not settings.v186_fast_pfok_enabled:
            return

        specs = [
            (
                InstrumentedProfitFOKEngineV186,
                "PFOK-FAST1",
                replace(settings, v185_base_latency_ms=settings.v186_fast1_latency_ms),
            ),
            (
                InstrumentedProfitFOKEngineV186,
                "PFOK-FAST2",
                replace(settings, v185_base_latency_ms=settings.v186_fast2_latency_ms),
            ),
            (
                RequoteProfitFOKEngineV186,
                "PFOK-REQUOTE1",
                replace(settings, v185_base_latency_ms=settings.v186_fast1_latency_ms),
            ),
            (
                RequoteProfitFOKEngineV186,
                "PFOK-REQUOTE2",
                replace(settings, v185_base_latency_ms=settings.v186_fast2_latency_ms),
            ),
            (
                InstrumentedProfitFOKEngineV186,
                "PFOK-FAST-S10",
                replace(
                    settings,
                    v185_base_latency_ms=settings.v186_fast_size_latency_ms,
                    v185_profit_sizes=(Decimal("10"),),
                ),
            ),
            (
                InstrumentedProfitFOKEngineV186,
                "PFOK-FAST-S20",
                replace(
                    settings,
                    v185_base_latency_ms=settings.v186_fast_size_latency_ms,
                    v185_profit_sizes=(Decimal("20"),),
                ),
            ),
        ]
        for cls, strategy, variant_settings in specs:
            self.variants.append(cls(variant_settings, recorder, strategy))

    def _build_snapshot(self, engine, market_id: str) -> SharedPFOKSnapshotV186 | None:
        pair = engine.pairs.get(market_id)
        if pair is None or market_phase(pair) != MarketPhase.LIVE:
            return None
        started = time.monotonic()
        a = engine.books.get(pair.token_a)
        b = engine.books.get(pair.token_b)
        if a is None or b is None or not a.ready or not b.ready:
            return SharedPFOKSnapshotV186(market_id, started, time.monotonic(), {})
        if (
            a.updated_monotonic <= 0
            or b.updated_monotonic <= 0
            or (started - a.updated_monotonic) * 1000 > self.settings.v185_max_book_age_ms
            or (started - b.updated_monotonic) * 1000 > self.settings.v185_max_book_age_ms
        ):
            return SharedPFOKSnapshotV186(market_id, started, time.monotonic(), {})

        metrics: dict[Decimal, dict[str, Any]] = {}
        for shares in sorted(set(self.settings.v186_fast_snapshot_sizes)):
            qa = a.quote_buy(shares)
            qb = b.quote_buy(shares)
            if qa is None or qb is None:
                continue
            fee_a = taker_fee(qa.segments, self.settings.crypto_taker_fee_rate)
            fee_b = taker_fee(qb.segments, self.settings.crypto_taker_fee_rate)
            pnl = shares - qa.notional - qb.notional - fee_a - fee_b
            coverage_a = sum((qty for px, qty in a.asks.items() if px <= qa.marginal_price), ZERO) / shares
            coverage_b = sum((qty for px, qty in b.asks.items() if px <= qb.marginal_price), ZERO) / shares
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
        return SharedPFOKSnapshotV186(market_id, started, time.monotonic(), metrics)

    def on_market_update(self, engine, market_id: str, surge=None) -> None:
        snapshot = self._build_snapshot(engine, market_id)
        if snapshot is None:
            return
        for variant in self.variants:
            variant.on_market_update_from_snapshot(engine, market_id, snapshot, surge)

    def process_due(self, engine) -> None:
        for variant in self.variants:
            variant.process_due(engine)

    def diagnostic_rows(self) -> list[dict[str, Any]]:
        return [variant.diagnostic_row() for variant in self.variants]

    def ranked_rows(self) -> list[dict[str, Any]]:
        rows = [row for row in self.diagnostic_rows() if row["placements"] > 0]
        return sorted(
            rows,
            key=lambda row: (row["ev_per_placement"], row["p_both"], -row["p_miss"]),
            reverse=True,
        )


_FAST_BY_RECORDER: dict[int, FastPFOKSuiteV186] = {}


class LatencyFirstMakerResearchSuiteV186:
    """Runs the latency frontier before the rest of the research stack timers."""

    def __init__(self, settings, recorder) -> None:
        self.base = WinnerResearchSuiteV184(settings, recorder)
        self.regime = self.base.regime
        self.fast = FastPFOKSuiteV186(settings, recorder)
        _FAST_BY_RECORDER[id(recorder)] = self.fast

    def __getattr__(self, name):
        return getattr(self.base, name)

    def process_due(self, engine) -> None:
        # Critical ordering: fast execution deadlines first, research second.
        self.fast.process_due(engine)
        self.base.process_due(engine)

    def on_market_update(self, engine, market_id: str) -> None:
        # Refresh the regime tracker first so the fast PFOK surge gate sees the
        # same state as the historical controls, then immediately evaluate the
        # shared fast snapshot before hedge/frontier/dual-FOK/atomic research.
        self.base.on_market_update(engine, market_id)
        surge = self.regime.current(market_id)
        self.fast.on_market_update(engine, market_id, surge)


class LatencyFirstDualFOKSuiteV186:
    """Existing seven 1.8.5 PFOK controls + 1.8.6 latency frontier view.

    Existing 1.8.5 models are instantiated from their original suite and their
    execution methods are not modified. Fast variants are executed earlier by
    LatencyFirstMakerResearchSuiteV186, but exposed here so the existing
    dashboard/reporting surfaces show all models together.
    """

    def __init__(self, settings, recorder) -> None:
        self.settings = settings
        self.control = DiagnosedProfitFOKSuiteV185(settings, recorder)
        self.fast = _FAST_BY_RECORDER.get(id(recorder))
        if self.fast is None:
            self.fast = FastPFOKSuiteV186(settings, recorder)
            _FAST_BY_RECORDER[id(recorder)] = self.fast

    def on_market_update(self, engine, market_id: str, surge=None) -> None:
        self.control.on_market_update(engine, market_id, surge)

    def process_due(self, engine) -> None:
        # Fast timers have already run first via the research wrapper.
        self.control.process_due(engine)

    def diagnostic_rows(self) -> list[dict[str, Any]]:
        return [*self.control.diagnostic_rows(), *self.fast.diagnostic_rows()]

    def ranked_rows(self) -> list[dict[str, Any]]:
        rows = [row for row in self.diagnostic_rows() if row["placements"] > 0]
        return sorted(
            rows,
            key=lambda row: (row["ev_per_placement"], row["p_both"], -row["p_miss"]),
            reverse=True,
        )
