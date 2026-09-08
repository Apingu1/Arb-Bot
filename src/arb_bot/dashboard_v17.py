from __future__ import annotations

import json
import logging
import threading
from collections import defaultdict
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .dashboard import DashboardServer, DashboardState, INDEX_HTML, ZERO, _jsonable
from .discovery import asset_from_slug


log = logging.getLogger(__name__)


def _stats() -> dict[str, Any]:
    return {"events": 0, "wins": 0, "losses": 0, "flats": 0}


def _family(strategy: str) -> str:
    if strategy.startswith("ATOMIC-"):
        return "ATOMIC"
    if strategy.startswith("PMAKER-"):
        return "PMAKER"
    for prefix in ("TAKER", "MAKER", "HYBRID", "HEDGE", "EV", "DFOK", "RFOK", "SPLITSELL"):
        if strategy == prefix or strategy.startswith(prefix + "-"):
            return prefix
    return "OTHER"


class DashboardStateV17(DashboardState):
    """Phase 1.7 dashboard accounting with session/all-time separation."""

    def __init__(self, settings) -> None:
        super().__init__(settings)
        self._session_pnl_by_strategy: dict[str, Decimal] = defaultdict(lambda: ZERO)
        self._session_pnl_by_asset: dict[str, Decimal] = defaultdict(lambda: ZERO)
        self._session_stats_by_strategy: dict[str, dict[str, Any]] = defaultdict(_stats)
        self._all_time_asset_strategy_pnl: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
        self._session_asset_strategy_pnl: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
        self._all_time_asset_strategy_stats: dict[tuple[str, str], dict[str, Any]] = defaultdict(_stats)
        self._session_asset_strategy_stats: dict[tuple[str, str], dict[str, Any]] = defaultdict(_stats)

    def bootstrap(self, path: str) -> None:
        super().bootstrap(path)
        # Historical events should establish all-time totals, not flood the live tape.
        with self._lock:
            self._events.clear()

    @staticmethod
    def _observe_stats(stats: dict[str, Any], pnl: Decimal) -> None:
        stats["events"] += 1
        if pnl > ZERO:
            stats["wins"] += 1
        elif pnl < ZERO:
            stats["losses"] += 1
        else:
            stats["flats"] += 1

    def _ingest(self, event_type: str, payload: dict[str, Any], *, historical: bool) -> None:
        if event_type == "strategy_equity":
            strategy = str(payload.get("strategy") or "UNKNOWN")
            pnl = Decimal(str(payload.get("pnl_delta") or "0"))
            slug = str(payload.get("slug") or "")
            asset = asset_from_slug(slug) or "UNKNOWN"
            key = (asset, strategy)
            with self._lock:
                self._all_time_asset_strategy_pnl[key] += pnl
                self._observe_stats(self._all_time_asset_strategy_stats[key], pnl)
                if not historical:
                    self._session_pnl_by_strategy[strategy] += pnl
                    self._session_pnl_by_asset[asset] += pnl
                    self._observe_stats(self._session_stats_by_strategy[strategy], pnl)
                    self._session_asset_strategy_pnl[key] += pnl
                    self._observe_stats(self._session_asset_strategy_stats[key], pnl)

        if event_type == "atomic_benchmark_capture" and not historical:
            pnl = Decimal(str(payload.get("realized_pnl") or "0"))
            strategy = str(payload.get("strategy") or "ATOMIC")
            asset = str(payload.get("asset") or asset_from_slug(str(payload.get("slug") or "")) or "UNKNOWN")
            self._push_event(
                {
                    "time": payload.get("captured_at"),
                    "kind": "ATOMIC_CAPTURE",
                    "strategy": strategy,
                    "family": "ATOMIC",
                    "asset": asset,
                    "slug": payload.get("slug"),
                    "status": "IDEAL_ONLY",
                    "action": f"perfect snapshot {float(pnl):+.5f} pUSD",
                    "pnl": float(pnl),
                    "counterfactual": True,
                }
            )

        super()._ingest(event_type, payload, historical=historical)

    def publish(
        self,
        engine,
        taker,
        edge_tracker,
        diagnostics,
        *,
        research=None,
        hedge=None,
        frontier=None,
        dual_fok=None,
        atomic=None,
        split_sell=None,
    ) -> dict[str, Any]:
        state = super().publish(
            engine,
            taker,
            edge_tracker,
            diagnostics,
            research=research,
            hedge=hedge,
            frontier=frontier,
            dual_fok=dual_fok,
            split_sell=split_sell,
        )

        with self._lock:
            all_time_total = Decimal(str(state.get("aggregate_shadow_pnl") or "0"))
            session_total = sum(self._session_pnl_by_strategy.values(), ZERO)

            strategies: list[dict[str, Any]] = []
            for row in state.get("strategies", []):
                row = dict(row)
                strategy = str(row.get("strategy") or "UNKNOWN")
                all_time_equity = Decimal(str(row.get("equity") or "0"))
                session_equity = self._session_pnl_by_strategy.get(strategy, ZERO)
                stats = self._session_stats_by_strategy.get(strategy, _stats())
                row["all_time_equity"] = float(all_time_equity)
                row["equity"] = float(session_equity)
                row["wins"] = int(stats.get("wins", 0))
                row["losses"] = int(stats.get("losses", 0))
                row["flats"] = int(stats.get("flats", 0))
                row["events"] = int(stats.get("events", 0))
                row["family"] = _family(strategy)
                if session_equity > ZERO:
                    row["state"] = "PROFIT"
                elif session_equity < ZERO:
                    row["state"] = "LOSS"
                elif int(row.get("placements") or 0) > 0:
                    row["state"] = "WAITING"
                else:
                    row["state"] = "IDLE"
                row["benchmark"] = False
                strategies.append(row)

            if atomic is not None:
                for raw in atomic.diagnostic_rows():
                    strategies.append(
                        {
                            "strategy": raw["strategy"],
                            "family": "ATOMIC",
                            "state": "BENCHMARK",
                            "equity": float(raw["benchmark_pnl"]),
                            "all_time_equity": float(raw["benchmark_pnl"]),
                            "events": raw["captures"],
                            "wins": raw["captures"],
                            "losses": 0,
                            "flats": 0,
                            "placements": raw["captures"],
                            "completed": raw["captures"],
                            "misses": 0,
                            "pending": raw["active_windows"],
                            "p_both": None,
                            "p_miss": None,
                            "ev_per_placement": float(raw["benchmark_pnl"] / Decimal(raw["captures"])) if raw["captures"] else 0.0,
                            "avg_lifetime_ms": float(raw["avg_lifetime_ms"]),
                            "median_lifetime_ms": float(raw["median_lifetime_ms"]),
                            "last_pnl": 0.0,
                            "last_status": "IDEAL_ONLY",
                            "last_action": "INSTANT_SIMULTANEOUS_COMPLETE_SET",
                            "benchmark": True,
                        }
                    )

            strategies.sort(key=lambda row: (bool(row.get("benchmark")), row.get("equity", 0)), reverse=True)

            assets: list[dict[str, Any]] = []
            for raw in state.get("assets", []):
                item = dict(raw)
                asset = str(item.get("asset") or "UNKNOWN")
                item["all_time_pnl"] = item.get("pnl", 0.0)
                item["pnl"] = float(self._session_pnl_by_asset.get(asset, ZERO))
                assets.append(item)

            asset_strategy_rows: list[dict[str, Any]] = []
            keys = set(self._all_time_asset_strategy_pnl) | set(self._session_asset_strategy_pnl)
            for asset, strategy in sorted(keys):
                session_stats = self._session_asset_strategy_stats.get((asset, strategy), _stats())
                all_stats = self._all_time_asset_strategy_stats.get((asset, strategy), _stats())
                asset_strategy_rows.append(
                    {
                        "asset": asset,
                        "strategy": strategy,
                        "family": _family(strategy),
                        "benchmark": False,
                        "session_pnl": float(self._session_asset_strategy_pnl.get((asset, strategy), ZERO)),
                        "all_time_pnl": float(self._all_time_asset_strategy_pnl.get((asset, strategy), ZERO)),
                        "session_wins": int(session_stats.get("wins", 0)),
                        "session_losses": int(session_stats.get("losses", 0)),
                        "all_time_events": int(all_stats.get("events", 0)),
                    }
                )
            if atomic is not None:
                for row in atomic.asset_rows():
                    asset_strategy_rows.append(
                        {
                            "asset": row["asset"],
                            "strategy": row["strategy"],
                            "family": "ATOMIC",
                            "benchmark": True,
                            "session_pnl": float(row["pnl"]),
                            "all_time_pnl": float(row["pnl"]),
                            "session_wins": int(row["captures"]),
                            "session_losses": 0,
                            "all_time_events": int(row["captures"]),
                        }
                    )
            asset_strategy_rows.sort(key=lambda row: (row["asset"], row["session_pnl"]), reverse=False)

            families: dict[str, dict[str, Any]] = {}
            for row in strategies:
                family = row["family"]
                info = families.setdefault(family, {"family": family, "pnl": 0.0, "models": 0, "wins": 0, "losses": 0, "benchmark": family == "ATOMIC"})
                info["pnl"] += float(row.get("equity") or 0.0)
                info["models"] += 1
                info["wins"] += int(row.get("wins") or 0)
                info["losses"] += int(row.get("losses") or 0)

            state["all_time_shadow_pnl"] = float(all_time_total)
            state["session_shadow_pnl"] = float(session_total)
            state["aggregate_shadow_pnl"] = float(session_total)
            state["aggregate_note"] = "primary balance is SESSION shadow P&L; ideal ATOMIC benchmark is excluded from executable-shadow totals"
            state["profitable_models"] = sum(1 for row in strategies if not row.get("benchmark") and row["equity"] > 0)
            state["losing_models"] = sum(1 for row in strategies if not row.get("benchmark") and row["equity"] < 0)
            state["flat_models"] = sum(1 for row in strategies if not row.get("benchmark") and row["equity"] == 0)
            state["strategies"] = strategies
            state["assets"] = assets
            state["asset_strategies"] = asset_strategy_rows
            state["families"] = sorted(families.values(), key=lambda item: item["pnl"], reverse=True)
            state["net_edge_shares"] = float(self.settings.min_trade_shares)
            self._state = state

        path = Path(self.settings.dashboard_state_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(_jsonable(state), separators=(",", ":")), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            log.debug("Could not write enhanced dashboard state snapshot: %s", exc)
        return _jsonable(state)


ENHANCED_HTML = INDEX_HTML
ENHANCED_HTML = ENHANCED_HTML.replace("grid-template-columns:repeat(5,minmax(160px,1fr))", "grid-template-columns:repeat(6,minmax(150px,1fr))")
ENHANCED_HTML = ENHANCED_HTML.replace("<div class=\"label\">Shadow Net</div>", "<div class=\"label\">Session Shadow P&L</div>")
ENHANCED_HTML = ENHANCED_HTML.replace(
    '<div class="kpi"><div class="label">Models + / −</div>',
    '<div class="kpi"><div class="label">All-Time Research P&L</div><div id="allTime" class="value">0.0000</div><div class="sub">historical shadow models · atomic excluded</div></div><div class="kpi"><div class="label">Models + / −</div>',
)
ENHANCED_HTML = ENHANCED_HTML.replace("5-share fee+risk adjusted pair edge", "configured-size fee/risk adjusted pair edge")
ENHANCED_HTML = ENHANCED_HTML.replace(
    '<section class="panel markets"><div class="panel-title"><span>LIVE / NEXT MARKET MATRIX</span>',
    '<section class="panel markets"><div class="panel-title"><span>ASSET × MODEL ATTRIBUTION</span><span class="label">session P&L · atomic marked ideal-only</span></div><div class="table-wrap"><table><thead><tr><th>ASSET</th><th>MODEL</th><th>TYPE</th><th>SESSION</th><th>ALL-TIME</th><th>W/L</th></tr></thead><tbody id="assetStrategyRows"></tbody></table></div></section><section class="panel markets"><div class="panel-title"><span>LIVE / NEXT MARKET MATRIX</span>',
)
ENHANCED_HTML = ENHANCED_HTML.replace(
    "function renderFilters(){",
    "function renderAssetStrategies(){let rows=(state.asset_strategies||[]).filter(r=>asset==='ALL'||r.asset===asset);$('assetStrategyRows').innerHTML=rows.map(r=>`<tr><td>${esc(r.asset)}</td><td class=\"strategy\">${esc(r.strategy)}</td><td>${r.benchmark?'IDEAL':'SHADOW'}</td><td class=\"${pnlClass(r.session_pnl)}\">${r.session_pnl>=0?'+':''}${f(r.session_pnl,4)}</td><td class=\"${pnlClass(r.all_time_pnl)}\">${r.all_time_pnl>=0?'+':''}${f(r.all_time_pnl,4)}</td><td>${r.session_wins}/${r.session_losses}</td></tr>`).join('')||'<tr><td colspan=\"6\" class=\"sub\">No attributed events yet.</td></tr>'}\nfunction renderFilters(){",
)
ENHANCED_HTML = ENHANCED_HTML.replace(
    "renderFilters();renderStrategies();renderEvents();renderMarkets();",
    "renderFilters();renderStrategies();renderEvents();renderAssetStrategies();renderMarkets();",
)
ENHANCED_HTML = ENHANCED_HTML.replace(
    "setText('net',(n>=0?'+':'')+f(n,4)+' pUSD',pnlClass(n));setText('models'",
    "setText('net',(n>=0?'+':'')+f(n,4)+' pUSD',pnlClass(n));let at=Number(state.all_time_shadow_pnl||0);setText('allTime',(at>=0?'+':'')+f(at,4)+' pUSD',pnlClass(at));setText('models'",
)


class DashboardServerV17(DashboardServer):
    def start(self) -> None:
        if self._server is not None:
            return
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def _send(self, status: int, body: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                if self.path in {"/", "/index.html"}:
                    self._send(200, ENHANCED_HTML.encode("utf-8"), "text/html; charset=utf-8")
                    return
                if self.path.startswith("/api/state"):
                    state = parent.state_getter() if parent.state_getter is not None else parent._read_state_file()
                    self._send(200, json.dumps(_jsonable(state), separators=(",", ":")).encode("utf-8"), "application/json")
                    return
                if self.path == "/health":
                    self._send(200, b'{"ok":true}', "application/json")
                    return
                self._send(404, b"not found", "text/plain; charset=utf-8")

        try:
            self._server = ThreadingHTTPServer((self.settings.dashboard_host, self.settings.dashboard_port), Handler)
        except OSError as exc:
            log.warning("Dashboard could not bind %s:%d: %s", self.settings.dashboard_host, self.settings.dashboard_port, exc)
            self._server = None
            return
        self._thread = threading.Thread(target=self._server.serve_forever, name="arb-dashboard", daemon=True)
        self._thread.start()
        log.info("ARB//TERM dashboard listening on http://%s:%d", self.settings.dashboard_host, self.settings.dashboard_port)
