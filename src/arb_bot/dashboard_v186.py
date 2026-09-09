from __future__ import annotations

import json
import logging
import threading
import time
from decimal import Decimal
from pathlib import Path

from .dashboard_v18 import DashboardServerV18, DashboardStateV18
from .discovery import asset_from_slug


log = logging.getLogger(__name__)
ZERO = Decimal("0")


class DashboardStateV186(DashboardStateV18):
    """1.8.6 dashboard state with non-blocking historical P&L bootstrap.

    The legacy dashboard replayed the complete shadow JSONL before market
    discovery/websocket streaming could begin. As the research file grew into a
    large multi-session dataset, startup could appear hung for many minutes.

    1.8.6 snapshots the historical file size at startup, returns immediately,
    and rebuilds only the historical strategy-equity aggregates in a background
    daemon thread. Live events are subscribed after ``bootstrap`` returns and
    therefore cannot be double-counted by the historical worker: the worker is
    capped at the frozen byte boundary captured before live streaming starts.
    """

    def __init__(self, settings) -> None:
        super().__init__(settings)
        self._history_loading = False
        self._history_bytes_total = 0
        self._history_bytes_read = 0
        self._history_equity_events = 0
        self._history_started_at = 0.0
        self._history_thread: threading.Thread | None = None

    def bootstrap(self, path: str) -> None:
        source = Path(path)
        if not source.exists():
            self._bootstrapped = True
            return

        try:
            snapshot_bytes = source.stat().st_size
        except OSError as exc:
            log.warning("Dashboard could not stat historical dataset %s: %s", source, exc)
            self._bootstrapped = True
            return

        if snapshot_bytes <= 0:
            self._bootstrapped = True
            return

        self._history_loading = True
        self._history_bytes_total = snapshot_bytes
        self._history_bytes_read = 0
        self._history_equity_events = 0
        self._history_started_at = time.monotonic()

        self._history_thread = threading.Thread(
            target=self._bootstrap_worker,
            args=(source, snapshot_bytes),
            name="arb-dashboard-history-v186",
            daemon=True,
        )
        self._history_thread.start()
        log.info(
            "ARB//TERM historical P&L bootstrap started in background | snapshot=%.1f MB",
            snapshot_bytes / (1024 * 1024),
        )

    def _bootstrap_worker(self, source: Path, snapshot_bytes: int) -> None:
        scanned = 0
        equity_events = 0
        last_progress_log = time.monotonic()

        try:
            with source.open("rb") as handle:
                while handle.tell() < snapshot_bytes:
                    raw = handle.readline()
                    if not raw:
                        break
                    scanned = min(handle.tell(), snapshot_bytes)

                    # Almost all historical rows are irrelevant to all-time
                    # dashboard P&L. Avoid json.loads for them entirely.
                    if b'"event_type":"strategy_equity"' not in raw and b'"event_type": "strategy_equity"' not in raw:
                        now = time.monotonic()
                        if now - last_progress_log >= 10:
                            self._set_history_progress(scanned, equity_events)
                            log.info(
                                "ARB//TERM historical P&L bootstrap %.1f%% | equity_events=%d",
                                (scanned / snapshot_bytes * 100) if snapshot_bytes else 100,
                                equity_events,
                            )
                            last_progress_log = now
                        continue

                    try:
                        row = json.loads(raw)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    if not isinstance(row, dict):
                        continue
                    payload = row.get("payload")
                    if not isinstance(payload, dict):
                        continue

                    self._ingest_historical_equity(payload)
                    equity_events += 1
                    self._set_history_progress(scanned, equity_events)

            elapsed = max(0.0, time.monotonic() - self._history_started_at)
            self._set_history_progress(snapshot_bytes, equity_events)
            self._bootstrapped = True
            log.info(
                "ARB//TERM historical P&L bootstrap complete | equity_events=%d elapsed=%.2fs snapshot=%.1f MB",
                equity_events,
                elapsed,
                snapshot_bytes / (1024 * 1024),
            )
        except OSError as exc:
            log.warning("Dashboard historical P&L bootstrap failed for %s: %s", source, exc)
            self._bootstrapped = True
        finally:
            self._history_loading = False

    def _set_history_progress(self, scanned: int, equity_events: int) -> None:
        with self._lock:
            self._history_bytes_read = scanned
            self._history_equity_events = equity_events

    def _ingest_historical_equity(self, payload: dict) -> None:
        strategy = str(payload.get("strategy") or "UNKNOWN")
        pnl = Decimal(str(payload.get("pnl_delta") or "0"))
        slug = str(payload.get("slug") or "")
        asset = asset_from_slug(slug) or "UNKNOWN"
        key = (asset, strategy)

        with self._lock:
            # DashboardState all-time aggregates.
            self._pnl_by_strategy[strategy] += pnl
            self._pnl_by_asset[asset] += pnl
            stats = self._stats_by_strategy[strategy]
            stats["events"] += 1
            stats["last_pnl"] = pnl
            stats["last_status"] = str(payload.get("status") or "UNKNOWN")
            stats["last_action"] = str(payload.get("action") or "")
            stats["last_slug"] = slug
            if pnl > ZERO:
                stats["wins"] += 1
            elif pnl < ZERO:
                stats["losses"] += 1
            else:
                stats["flats"] += 1

            # DashboardStateV17 asset × strategy all-time aggregates. Session
            # dictionaries are deliberately untouched for historical rows.
            self._all_time_asset_strategy_pnl[key] += pnl
            self._observe_stats(self._all_time_asset_strategy_stats[key], pnl)

    def publish(self, *args, **kwargs):
        state = super().publish(*args, **kwargs)
        with self._lock:
            total = self._history_bytes_total
            read = self._history_bytes_read
            state["history_bootstrap"] = {
                "loading": self._history_loading,
                "complete": self._bootstrapped,
                "bytes_read": read,
                "bytes_total": total,
                "progress_pct": (read / total * 100) if total else 100.0,
                "equity_events": self._history_equity_events,
            }
            self._state = state
        return state


# Server behaviour is unchanged; only state bootstrap semantics differ.
DashboardServerV186 = DashboardServerV18
