from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any

from .storage import JsonlRecorder


CRITICAL_EVENTS = {
    "profit_fok_candidate_v185",
    "profit_fok_preflight_reject_v185",
    "dual_fok_attempt_placed",
    "profit_fok_leg_result_v185",
    "dual_fok_execution_summary",
    "strategy_equity",
    "pfok_latency_v186",
}


class LowLatencyJsonlRecorderV186(JsonlRecorder):
    """Persistent buffered recorder with compact PFOK gate rollups.

    Phase 1.8.5 opened/wrote/closed the JSONL file for every event. During a
    three-hour frontier run that included >270k gate samples, synchronous file
    churn competed with millisecond execution timers. 1.8.6 keeps append
    handles open, flushes important strategy events immediately, and converts
    raw gate samples into exact-count periodic rollups.

    Phase 1.8.7 also contains the deliberately hyper-active BFOK-RAW diagnostic.
    Per-attempt RAW events are *not* persisted to JSONL because RAW can generate
    hundreds of thousands of executions in minutes and `strategy_equity` is a
    critical/flush-on-write event. Persisting those events caused multi-GB
    session files and, more importantly, synchronous disk flushes on the same
    latency-sensitive process being measured. RAW events are still forwarded to
    live listeners (so the dashboard can update), while durable RAW statistics
    come from compact opportunity-funnel rollups and sparse RAW-win attribution
    events.

    A second buffered current-session JSONL is maintained so `arb-report
    --session` can avoid rescanning the full historical file. It contains only
    the current process and is truncated at recorder construction.
    """

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._listeners = []

        # Settings are not passed into recorder construction by the legacy main
        # loop, so read the already-loaded environment through SettingsV186.
        from .config_v186 import SettingsV186

        settings = SettingsV186()
        self._flush_interval = max(0.01, settings.v186_recorder_flush_ms / 1000)
        self._rollup_interval = max(1.0, float(settings.v186_gate_rollup_seconds))
        self._sample_cap = max(16, int(settings.v186_rollup_sample_cap))
        self.session_path = Path(settings.v186_current_session_path)
        self.session_path.parent.mkdir(parents=True, exist_ok=True)

        self._main = self.path.open("a", encoding="utf-8", buffering=65536)
        self._session = self.session_path.open("w", encoding="utf-8", buffering=65536)
        self._next_flush = time.monotonic() + self._flush_interval
        self._next_rollup = time.monotonic() + self._rollup_interval
        self._gate: dict[str, dict[str, Any]] = defaultdict(self._new_gate_bucket)

    @staticmethod
    def _new_gate_bucket() -> dict[str, Any]:
        return {
            "samples": 0,
            "reasons": Counter(),
            "edges": [],
            "coverage": [],
            "ages": [],
            "context": {},
        }

    @staticmethod
    def _median(values: list[float]) -> float | None:
        return float(median(values)) if values else None

    def _notify(self, event_type: str, payload: Any) -> None:
        for listener in tuple(self._listeners):
            try:
                listener(event_type, payload)
            except Exception:
                continue

    def _raw_write(self, event_type: str, payload: Any, *, notify: bool = True) -> None:
        row = {"event_type": event_type, "payload": payload}
        encoded = json.dumps(row, default=self._default, separators=(",", ":")) + "\n"
        self._main.write(encoded)
        self._session.write(encoded)
        if notify:
            self._notify(event_type, payload)

    def _append_sample(self, target: list[float], raw: Any) -> None:
        if raw is None or len(target) >= self._sample_cap:
            return
        try:
            target.append(float(raw))
        except (TypeError, ValueError):
            return

    def _collect_gate(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        strategy = str(payload.get("strategy") or "UNKNOWN")
        bucket = self._gate[strategy]
        bucket["samples"] += 1
        bucket["reasons"][str(payload.get("gate_reason") or "UNKNOWN")] += 1
        self._append_sample(bucket["edges"], payload.get("sample_edge_per_share"))
        self._append_sample(bucket["coverage"], payload.get("sample_coverage"))
        self._append_sample(bucket["ages"], payload.get("book_age_a_ms"))
        self._append_sample(bucket["ages"], payload.get("book_age_b_ms"))
        bucket["context"] = {
            "phase183_run_id": payload.get("phase183_run_id"),
            "phase184_run_id": payload.get("phase184_run_id"),
            "phase185_run_id": payload.get("phase185_run_id"),
            "strategy": strategy,
            "mode": payload.get("mode"),
        }

    def _flush_gate_rollups(self, now: float) -> None:
        if not self._gate:
            self._next_rollup = now + self._rollup_interval
            return
        for strategy, bucket in list(self._gate.items()):
            if not bucket["samples"]:
                continue
            payload = {
                **bucket["context"],
                "strategy": strategy,
                "rollup_seconds": self._rollup_interval,
                "samples": bucket["samples"],
                "gate_reasons": dict(bucket["reasons"]),
                "p50_edge_per_share": self._median(bucket["edges"]),
                "p50_coverage": self._median(bucket["coverage"]),
                "p50_book_age_ms": self._median(bucket["ages"]),
                "sampled_values": min(self._sample_cap, len(bucket["edges"])),
            }
            self._raw_write("profit_fok_gate_rollup_v186", payload, notify=False)
        self._gate.clear()
        self._next_rollup = now + self._rollup_interval

    def flush(self) -> None:
        now = time.monotonic()
        self._flush_gate_rollups(now)
        self._main.flush()
        self._session.flush()
        self._next_flush = now + self._flush_interval

    def write(self, event_type: str, payload: Any) -> None:
        now = time.monotonic()

        # BFOK-RAW is intentionally capable of firing on nearly every market
        # update. Keep it off the durable hot path. The opportunity funnel owns
        # compact persistent RAW counts/P&L and raw_win_attribution_v187 owns the
        # sparse detailed winning cases. We still notify live listeners so the
        # existing dashboard remains useful during a run.
        if isinstance(payload, dict) and payload.get("strategy") == "BFOK-RAW":
            self._notify(event_type, payload)
            if now >= self._next_rollup:
                self._flush_gate_rollups(now)
            if now >= self._next_flush:
                self._main.flush()
                self._session.flush()
                self._next_flush = now + self._flush_interval
            return

        if event_type == "profit_fok_gate_sample_v185":
            self._collect_gate(payload)
            if now >= self._next_rollup:
                self._flush_gate_rollups(now)
            if now >= self._next_flush:
                self._main.flush()
                self._session.flush()
                self._next_flush = now + self._flush_interval
            return

        if now >= self._next_rollup:
            self._flush_gate_rollups(now)
        self._raw_write(event_type, payload)

        if event_type in CRITICAL_EVENTS or now >= self._next_flush:
            self._main.flush()
            self._session.flush()
            self._next_flush = now + self._flush_interval

    def close(self) -> None:
        try:
            self.flush()
        finally:
            try:
                self._main.close()
            finally:
                self._session.close()

    def __del__(self) -> None:
        try:
            if hasattr(self, "_main") and not self._main.closed:
                self.close()
        except Exception:
            pass
