from __future__ import annotations

from decimal import Decimal

from .batch_fok_v187 import PreciseBatchFOKSuiteV187, SharedBatchSnapshotV187
from .latency_frontier_v189 import LatencyFrontierSuiteV189
from .maker_research import MarketRegimeTracker
from .raw_observer_v189 import RawOpportunityObserverV189
from .runtime_v187 import CorePFOKSuiteV187


ZERO = Decimal("0")


class ProtectedBatchSuiteV189:
    """Unchanged protected BFOK controls with due work serviced first."""

    def __init__(self, settings, recorder) -> None:
        self.settings = settings
        self.base = PreciseBatchFOKSuiteV187(settings, recorder)
        self.variants = self.base.variants
        self.risk_book = self.base.risk_book
        self.last_standard_snapshot: SharedBatchSnapshotV187 | None = None

    def on_market_update(self, engine, market_id: str, surge=None) -> None:
        # Service previously-due arrivals before doing any new snapshot/research
        # work. This makes protected execution the hot-path priority.
        self.base.process_due(engine)
        snapshot = self.base._build_snapshot(engine, market_id)
        self.last_standard_snapshot = snapshot
        if snapshot is not None:
            for variant in self.base.variants:
                variant.on_market_update_from_snapshot(snapshot, surge)
            self.base._arm_timer(engine)

    def process_due(self, engine) -> None:
        self.base.process_due(engine)

    def diagnostic_rows(self):
        return self.base.diagnostic_rows()

    def ranked_rows(self):
        return self.base.ranked_rows()


class InactiveAuxiliarySuiteV189:
    """No-op for unrelated maker/hedge/EV research during latency isolation."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def on_market_update(self, *args, **kwargs) -> None:
        return None

    def process_due(self, *args, **kwargs) -> None:
        return None

    def diagnostic_rows(self):
        return []

    def ranked_rows(self):
        return []


_BATCH_BY_RECORDER: dict[int, ProtectedBatchSuiteV189] = {}
_LATENCY_BY_RECORDER: dict[int, LatencyFrontierSuiteV189] = {}
_RAW_BY_RECORDER: dict[int, RawOpportunityObserverV189] = {}


class LatencyIsolationResearchSuiteV189:
    """Minimal research surface: surge tracker + BFOK + latency + RAW observer.

    Historical maker/hybrid engines are intentionally not instantiated in this
    phase. The sole purpose is to reduce local event-loop work while preserving
    the protected BFOK controls and the shared surge detector.
    """

    def __init__(self, settings, recorder) -> None:
        self.settings = settings
        self.recorder = recorder
        self.regime = MarketRegimeTracker(settings)
        self.batch = ProtectedBatchSuiteV189(settings, recorder)
        self.latency = LatencyFrontierSuiteV189(settings, recorder)
        self.raw = RawOpportunityObserverV189(settings, recorder)
        _BATCH_BY_RECORDER[id(recorder)] = self.batch
        _LATENCY_BY_RECORDER[id(recorder)] = self.latency
        _RAW_BY_RECORDER[id(recorder)] = self.raw

    def on_market_update(self, engine, market_id: str) -> None:
        # Due arrivals always win CPU priority over new diagnostics.
        self.latency.process_due(engine)
        self.batch.process_due(engine)
        surge = self.regime.observe(engine, market_id)

        # Protected BFOK constructs the one shared fresh snapshot first.
        self.batch.on_market_update(engine, market_id, surge)

        # The controlled latency frontier reuses that protected snapshot; no
        # extra order-book quote is required for each latency target.
        self.latency.on_market_update(
            engine,
            market_id,
            surge,
            self.batch.last_standard_snapshot,
        )

        # Observation-only RAW is deliberately last on the hot path.
        self.raw.on_market_update(engine, market_id)

    def process_due(self, engine) -> None:
        self.latency.process_due(engine)
        self.batch.process_due(engine)
        self.raw.process_due(engine)

    def diagnostic_rows(self):
        # Actual BFOK/latency rows are surfaced through DualFOK below so they
        # appear once in the dashboard rather than being duplicated here.
        return []


class ParallelBatchFOKSuiteV189:
    """PFOK controls plus shared protected BFOK/latency/RAW diagnostic rows."""

    def __init__(self, settings, recorder) -> None:
        self.settings = settings
        self.control = CorePFOKSuiteV187(settings, recorder)
        self.batch = _BATCH_BY_RECORDER.get(id(recorder))
        self.latency = _LATENCY_BY_RECORDER.get(id(recorder))
        self.raw = _RAW_BY_RECORDER.get(id(recorder))
        if self.batch is None:
            self.batch = ProtectedBatchSuiteV189(settings, recorder)
            _BATCH_BY_RECORDER[id(recorder)] = self.batch
        if self.latency is None:
            self.latency = LatencyFrontierSuiteV189(settings, recorder)
            _LATENCY_BY_RECORDER[id(recorder)] = self.latency
        if self.raw is None:
            self.raw = RawOpportunityObserverV189(settings, recorder)
            _RAW_BY_RECORDER[id(recorder)] = self.raw

    def on_market_update(self, engine, market_id: str, surge=None) -> None:
        # PFOK remains an unchanged sequential control. Service existing work
        # before detection so it does not wait behind a new market update.
        self.control.process_due(engine)
        self.control.on_market_update(engine, market_id, surge)

    def process_due(self, engine) -> None:
        self.control.process_due(engine)

    def _batch_row(self, variant) -> dict:
        row = dict(variant.diagnostic_row())
        miss_values = list(getattr(variant, "miss_losses_per_share", []))
        avg_miss = sum(miss_values, ZERO) / Decimal(len(miss_values)) if miss_values else ZERO
        row.update(
            {
                "opportunities": int(row.get("candidates") or 0),
                "lifetime_samples": int(row.get("completed") or 0),
                "shares": (
                    format(variant.fixed_size.normalize(), "f")
                    if variant.fixed_size is not None
                    else "EV"
                ),
                "edge_target": self.settings.v187_detection_min_edge_per_share,
                "coverage_multiple": self.settings.v187_detection_coverage_multiple,
                "base_latency_ms": self.settings.v187_batch_arrival_latency_ms,
                "avg_miss_loss_per_share": avg_miss,
                "recovery_completions": variant.recovery_completions,
                "recovery_unwinds": variant.recovery_unwinds,
                "recovery_liquidity_failures": variant.recovery_liquidity_failures,
                "surge_blocks": variant.surge_blocks,
            }
        )
        return row

    def diagnostic_rows(self):
        batch_rows = [self._batch_row(v) for v in self.batch.variants]
        return [
            *self.control.diagnostic_rows(),
            *batch_rows,
            *self.latency.diagnostic_rows(),
            self.raw.diagnostic_row(),
        ]

    def ranked_rows(self):
        rows = [
            row
            for row in self.diagnostic_rows()
            if row.get("placements", 0) > 0 and row.get("direction") != "OBSERVE_ONLY"
        ]
        return sorted(
            rows,
            key=lambda row: (row.get("ev_per_placement", ZERO), row.get("p_both", ZERO)),
            reverse=True,
        )
