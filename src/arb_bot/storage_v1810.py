from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config_v1810 import SettingsV1810
from .research_context_v1810 import PHASE1810_RUN_ID
from .storage_v186 import LowLatencyJsonlRecorderV186


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


EPISODE_EVENTS = {
    "raw_positive_observation_v189",
    "profit_fok_candidate_v185",
    "profit_fok_preflight_reject_v185",
    "dual_fok_attempt_placed",
    "dual_fok_execution_summary",
    "batch_fok_candidate_v187",
    "batch_fok_submission_v187",
    "batch_fok_execution_summary_v187",
    "latency_candidate_v189",
    "latency_execution_v189",
}
PHASE1810_ARCHIVE_EVENTS = EPISODE_EVENTS | {"raw_observer_rollup_v189"}


class LowLatencyJsonlRecorderV1810(LowLatencyJsonlRecorderV186):
    """Run-aware recorder with shared opportunity IDs and durable run files.

    A single episode ID is shared across PFOK, BFOK, latency and RAW records on
    the same market while qualifying activity remains within the configured
    clustering window.  This fixes the historical report behaviour that
    treated every unlabelled profitable execution as independent.
    """

    def __init__(self, path: str) -> None:
        super().__init__(path)
        settings = SettingsV1810()
        archive_dir = Path(settings.v1810_session_archive_dir)
        archive_dir.mkdir(parents=True, exist_ok=True)
        self.archive_path = archive_dir / f"phase1810_{PHASE1810_RUN_ID}.jsonl"
        self._archive = self.archive_path.open("a", encoding="utf-8", buffering=65536)
        self._episode_window = max(1, settings.v1810_episode_window_ms) / 1000
        self._episodes: dict[str, tuple[float, str]] = {}
        self._episode_sequence = 0

    def _episode_id(self, market_id: str, now: float) -> str:
        previous = self._episodes.get(market_id)
        if previous is not None and now - previous[0] <= self._episode_window:
            episode_id = previous[1]
        else:
            self._episode_sequence += 1
            episode_id = f"1810-{PHASE1810_RUN_ID}-{self._episode_sequence:06d}"
        self._episodes[market_id] = (now, episode_id)
        return episode_id

    def _enrich(self, event_type: str, payload: Any) -> Any:
        if not isinstance(payload, dict):
            return payload
        # Keep timestamping and archive work off unrelated high-volume events;
        # Phase 1.8.10 must not contaminate the latency experiment it measures.
        if event_type not in PHASE1810_ARCHIVE_EVENTS:
            return payload
        enriched = dict(payload)
        enriched.setdefault("phase1810_run_id", PHASE1810_RUN_ID)
        enriched.setdefault("phase_version", "1.8.10")
        enriched.setdefault("recorded_at", _utc_now())
        market_id = str(enriched.get("market_id") or "")
        if market_id and event_type in EPISODE_EVENTS:
            enriched.setdefault("market_episode_id", self._episode_id(market_id, time.monotonic()))
        return enriched

    def _raw_write(self, event_type: str, payload: Any, *, notify: bool = True) -> None:
        enriched = self._enrich(event_type, payload)
        super()._raw_write(event_type, enriched, notify=notify)
        if event_type in PHASE1810_ARCHIVE_EVENTS:
            row = {"event_type": event_type, "payload": enriched}
            self._archive.write(json.dumps(row, default=self._default, separators=(",", ":")) + "\n")

    def flush(self) -> None:
        super().flush()
        if hasattr(self, "_archive") and not self._archive.closed:
            self._archive.flush()

    def close(self) -> None:
        if not hasattr(self, "_main") or self._main.closed:
            return
        try:
            self.flush()
        finally:
            self._main.close()
            self._session.close()
            if hasattr(self, "_archive") and not self._archive.closed:
                self._archive.close()
