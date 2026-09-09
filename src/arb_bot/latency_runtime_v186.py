from __future__ import annotations

import asyncio
import time

from .profit_fok_v185_diag import DiagnosedProfitFOKSuiteV185
from .profit_fok_v186 import FastPFOKSuiteV186
from .winner_research_v184 import WinnerResearchSuiteV184


class PreciseFastPFOKSuiteV186(FastPFOKSuiteV186):
    """Fast PFOK suite with a dedicated earliest-deadline asyncio timer.

    The legacy runtime polls every ~1 ms. A 1 ms preflight created just after a
    poll can therefore wait nearly another full polling interval. This suite
    arms one event-loop timer at the earliest PFOK preflight/second/recovery due
    time and re-arms after each state transition. The normal polling loop stays
    in place as a safety net.
    """

    def __init__(self, settings, recorder) -> None:
        super().__init__(settings, recorder)
        self._timer_handle: asyncio.TimerHandle | None = None
        self._timer_deadline: float | None = None
        self._timer_engine = None

    def _next_due(self) -> float | None:
        due: list[float] = []
        for variant in self.variants:
            for pending in variant.pending.values():
                if pending.first_fill is None:
                    due.append(pending.preflight_due)
                elif pending.second_fill is None and pending.recovery_due is None:
                    if pending.second_due is not None:
                        due.append(pending.second_due)
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
        delay = max(0.0, target - time.monotonic())
        self._timer_deadline = target
        self._timer_handle = loop.call_later(delay, self._timer_fire)

    def _timer_fire(self) -> None:
        engine = self._timer_engine
        self._timer_handle = None
        self._timer_deadline = None
        if engine is None:
            return
        # Call the parent implementation directly so this callback cannot
        # recursively schedule before all variants have advanced one state.
        super().process_due(engine)
        self._arm_timer(engine)

    def on_market_update(self, engine, market_id: str, surge=None) -> None:
        super().on_market_update(engine, market_id, surge)
        self._arm_timer(engine)

    def process_due(self, engine) -> None:
        super().process_due(engine)
        self._arm_timer(engine)


_FAST_BY_RECORDER: dict[int, PreciseFastPFOKSuiteV186] = {}


class LatencyFirstMakerResearchSuiteV186:
    """Existing maker/regime research plus the prioritized precise PFOK path."""

    def __init__(self, settings, recorder) -> None:
        self.base = WinnerResearchSuiteV184(settings, recorder)
        self.regime = self.base.regime
        self.fast = PreciseFastPFOKSuiteV186(settings, recorder)
        _FAST_BY_RECORDER[id(recorder)] = self.fast

    def __getattr__(self, name):
        return getattr(self.base, name)

    def process_due(self, engine) -> None:
        self.fast.process_due(engine)
        self.base.process_due(engine)

    def on_market_update(self, engine, market_id: str) -> None:
        self.base.on_market_update(engine, market_id)
        surge = self.regime.current(market_id)
        self.fast.on_market_update(engine, market_id, surge)


class LatencyFirstDualFOKSuiteV186:
    """Unchanged 1.8.5 controls plus the precise 1.8.6 latency frontier."""

    def __init__(self, settings, recorder) -> None:
        self.settings = settings
        self.control = DiagnosedProfitFOKSuiteV185(settings, recorder)
        self.fast = _FAST_BY_RECORDER.get(id(recorder))
        if self.fast is None:
            self.fast = PreciseFastPFOKSuiteV186(settings, recorder)
            _FAST_BY_RECORDER[id(recorder)] = self.fast

    def on_market_update(self, engine, market_id: str, surge=None) -> None:
        self.control.on_market_update(engine, market_id, surge)

    def process_due(self, engine) -> None:
        self.control.process_due(engine)

    def diagnostic_rows(self):
        return [*self.control.diagnostic_rows(), *self.fast.diagnostic_rows()]

    def ranked_rows(self):
        rows = [row for row in self.diagnostic_rows() if row["placements"] > 0]
        return sorted(
            rows,
            key=lambda row: (row["ev_per_placement"], row["p_both"], -row["p_miss"]),
            reverse=True,
        )
