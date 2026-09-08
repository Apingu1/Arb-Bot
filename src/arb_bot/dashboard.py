from __future__ import annotations

import json
import logging
import threading
import time
from collections import defaultdict, deque
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from .discovery import MarketPhase, asset_from_slug, market_phase, updown_15m_window_from_slug
from .fees import taker_fee


log = logging.getLogger(__name__)
ZERO = Decimal("0")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _d(value: Any) -> Decimal:
    if value is None or value == "":
        return ZERO
    return Decimal(str(value))


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, deque)):
        return [_jsonable(v) for v in value]
    return value


def _family(strategy: str) -> str:
    for prefix in ("TAKER", "MAKER", "HYBRID", "HEDGE", "EV", "DFOK", "RFOK", "SPLITSELL"):
        if strategy == prefix or strategy.startswith(prefix + "-"):
            return prefix
    return "OTHER"


def _fmt_decimal(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


class DashboardState:
    """Live read model for the retro terminal UI.

    Strategy P&L is reconstructed from strategy_equity events in the active JSONL
    dataset. The headline total is intentionally an aggregate of independent
    counterfactual shadow models, not an executable account balance.
    """

    def __init__(self, settings) -> None:
        self.settings = settings
        self._lock = threading.RLock()
        self._events: deque[dict[str, Any]] = deque(maxlen=max(20, settings.dashboard_event_limit))
        self._pnl_by_strategy: dict[str, Decimal] = defaultdict(lambda: ZERO)
        self._pnl_by_asset: dict[str, Decimal] = defaultdict(lambda: ZERO)
        self._stats_by_strategy: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"events": 0, "wins": 0, "losses": 0, "flats": 0, "last_pnl": ZERO, "last_status": None, "last_action": None, "last_slug": None}
        )
        self._state: dict[str, Any] = {
            "generated_at": _utc_now(),
            "mode": "SHADOW_ONLY",
            "aggregate_shadow_pnl": 0.0,
            "aggregate_note": "sum of independent counterfactual shadow models; not one executable account",
            "assets": [],
            "markets": [],
            "strategies": [],
            "families": [],
            "events": [],
            "feed": {},
        }
        self._last_message_count = 0
        self._last_message_sample_at = time.monotonic()
        self._message_rate = 0.0
        self._bootstrapped = False

    def bootstrap(self, path: str) -> None:
        source = Path(path)
        if not source.exists():
            self._bootstrapped = True
            return
        try:
            with source.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(row, dict):
                        continue
                    payload = row.get("payload")
                    if isinstance(payload, dict):
                        self._ingest(str(row.get("event_type") or ""), payload, historical=True)
            self._bootstrapped = True
        except OSError as exc:
            log.warning("Dashboard could not bootstrap %s: %s", source, exc)
            self._bootstrapped = True

    def on_event(self, event_type: str, payload: Any) -> None:
        if not isinstance(payload, dict):
            if is_dataclass(payload):
                payload = asdict(payload)
            else:
                return
        self._ingest(event_type, payload, historical=False)

    def _push_event(self, event: dict[str, Any]) -> None:
        self._events.appendleft(_jsonable(event))

    def _ingest(self, event_type: str, payload: dict[str, Any], *, historical: bool) -> None:
        with self._lock:
            if event_type == "strategy_equity":
                strategy = str(payload.get("strategy") or "UNKNOWN")
                pnl = _d(payload.get("pnl_delta"))
                slug = str(payload.get("slug") or "")
                asset = asset_from_slug(slug) or "UNKNOWN"
                status = str(payload.get("status") or "UNKNOWN")
                action = str(payload.get("action") or "")
                recorded_at = str(payload.get("recorded_at") or _utc_now())

                self._pnl_by_strategy[strategy] += pnl
                self._pnl_by_asset[asset] += pnl
                stats = self._stats_by_strategy[strategy]
                stats["events"] += 1
                stats["last_pnl"] = pnl
                stats["last_status"] = status
                stats["last_action"] = action
                stats["last_slug"] = slug
                if pnl > ZERO:
                    stats["wins"] += 1
                elif pnl < ZERO:
                    stats["losses"] += 1
                else:
                    stats["flats"] += 1

                if pnl >= self.settings.dashboard_major_win_usdc:
                    kind = "MAJOR_WIN"
                elif pnl > ZERO:
                    kind = "WIN"
                elif pnl <= -self.settings.dashboard_major_loss_usdc:
                    kind = "MAJOR_LOSS"
                elif pnl < ZERO:
                    kind = "LOSS"
                else:
                    kind = "FLAT"
                self._push_event(
                    {
                        "time": recorded_at,
                        "kind": kind,
                        "strategy": strategy,
                        "family": _family(strategy),
                        "asset": asset,
                        "slug": slug,
                        "status": status,
                        "action": action,
                        "pnl": float(pnl),
                        "counterfactual": False,
                    }
                )
                return

            if event_type == "hedge_ghost_outcome" and payload.get("outcome") == "WOULD_FILL":
                pnl = _d(payload.get("best_recovery_pnl"))
                profitable = bool(payload.get("would_be_profitable"))
                if not profitable and pnl > -self.settings.dashboard_major_loss_usdc:
                    return
                strategy = str(payload.get("strategy") or "EV-GHOST")
                slug = str(payload.get("slug") or "")
                self._push_event(
                    {
                        "time": _utc_now(),
                        "kind": "GHOST_WIN" if profitable else "GHOST_TOXIC",
                        "strategy": strategy,
                        "family": _family(strategy),
                        "asset": asset_from_slug(slug) or "UNKNOWN",
                        "slug": slug,
                        "status": "WOULD_FILL",
                        "action": "BEST_RECOVERY_COUNTERFACTUAL",
                        "pnl": float(pnl),
                        "counterfactual": True,
                    }
                )
                return

            if event_type == "dual_fok_opportunity_lifetime":
                lifetime = _d(payload.get("lifetime_ms"))
                peak_edge = _d(payload.get("peak_edge_per_share"))
                armed = bool(payload.get("attempt_armed"))
                if not armed and peak_edge < Decimal("0.01"):
                    return
                strategy = str(payload.get("strategy") or "DFOK")
                slug = str(payload.get("slug") or "")
                self._push_event(
                    {
                        "time": str(payload.get("ended_at") or _utc_now()),
                        "kind": "FLEETING_ARB" if lifetime <= Decimal("25") else "ARB_WINDOW",
                        "strategy": strategy,
                        "family": _family(strategy),
                        "asset": asset_from_slug(slug) or "UNKNOWN",
                        "slug": slug,
                        "status": str(payload.get("end_reason") or "ENDED"),
                        "action": f"life={float(lifetime):.1f}ms peak_edge={float(peak_edge):+.4f}/sh",
                        "pnl": None,
                        "counterfactual": True,
                    }
                )

    def _strategy_rows(self, taker, research, hedge, frontier, dual_fok, split_sell=None) -> list[dict[str, Any]]:
        raw_rows: list[dict[str, Any]] = [
            {
                "strategy": "TAKER",
                "mode": "TAKER",
                "pending": taker.pending_count,
                "completed": taker.completed,
                "misses": taker.leg_misses,
                "rejected": taker.rejected,
                "placements": taker.empirical_risk.attempts,
                "p_miss": taker.empirical_risk.miss_probability,
                "live_equity": taker.total_pnl,
            }
        ]
        if research is not None:
            raw_rows.extend(research.diagnostic_rows())
        if hedge is not None:
            raw_rows.extend(hedge.diagnostic_rows())
        if frontier is not None:
            raw_rows.extend(frontier.diagnostic_rows())
        if dual_fok is not None:
            raw_rows.extend(dual_fok.diagnostic_rows())
        if split_sell is not None:
            raw_rows.extend(split_sell.diagnostic_rows())

        result: list[dict[str, Any]] = []
        for raw in raw_rows:
            strategy = str(raw.get("strategy") or "UNKNOWN")
            stats = dict(self._stats_by_strategy.get(strategy) or {})
            dataset_equity = self._pnl_by_strategy.get(strategy, ZERO)
            events = int(stats.get("events") or 0)
            if dataset_equity > ZERO:
                state = "PROFIT"
            elif dataset_equity < ZERO:
                state = "LOSS"
            elif events:
                state = "FLAT"
            elif int(raw.get("placements") or raw.get("placed") or 0) > 0:
                state = "WAITING"
            else:
                state = "IDLE"

            placements = int(raw.get("placements") or raw.get("placed") or 0)
            completed = int(raw.get("completed") or raw.get("both_filled") or raw.get("hedge_successes") or 0)
            misses = int(raw.get("misses") or raw.get("one_leg_miss") or raw.get("inventory_exits") or raw.get("hedge_misses") or 0)
            p_both = raw.get("p_both")
            p_miss = raw.get("p_miss")
            ev_place = raw.get("ev_per_placement") or raw.get("realized_ev_per_placement") or ZERO
            row = {
                "strategy": strategy,
                "family": _family(strategy),
                "state": state,
                "equity": float(dataset_equity),
                "events": events,
                "wins": int(stats.get("wins") or 0),
                "losses": int(stats.get("losses") or 0),
                "flats": int(stats.get("flats") or 0),
                "placements": placements,
                "completed": completed,
                "misses": misses,
                "pending": int(raw.get("pending") or 0),
                "p_both": _fmt_decimal(_d(p_both)) if p_both is not None else None,
                "p_miss": _fmt_decimal(_d(p_miss)) if p_miss is not None else None,
                "ev_per_placement": float(_d(ev_place)),
                "sample_status": raw.get("sample_status"),
                "avg_lifetime_ms": float(_d(raw.get("avg_lifetime_ms"))) if raw.get("avg_lifetime_ms") is not None else None,
                "median_lifetime_ms": float(_d(raw.get("median_lifetime_ms"))) if raw.get("median_lifetime_ms") is not None else None,
                "last_pnl": float(_d(stats.get("last_pnl"))) if stats else 0.0,
                "last_status": stats.get("last_status"),
                "last_action": stats.get("last_action"),
            }
            result.append(row)
        result.sort(key=lambda row: (row["equity"], row["wins"], -row["losses"]), reverse=True)
        return result

    def publish(self, engine, taker, edge_tracker, diagnostics, *, research=None, hedge=None, frontier=None, dual_fok=None, split_sell=None) -> dict[str, Any]:
        now = time.monotonic()
        now_utc = datetime.now(timezone.utc)
        with self._lock:
            elapsed = max(now - self._last_message_sample_at, 0.001)
            message_count = int(getattr(diagnostics, "total_messages", 0))
            delta = max(0, message_count - self._last_message_count)
            self._message_rate = delta / elapsed
            self._last_message_count = message_count
            self._last_message_sample_at = now

            markets: list[dict[str, Any]] = []
            live_assets: set[str] = set()
            for pair in engine.pairs.values():
                phase = market_phase(pair, now_utc)
                if phase not in {MarketPhase.LIVE, MarketPhase.NEXT}:
                    continue
                asset = asset_from_slug(pair.slug) or "UNKNOWN"
                if phase is MarketPhase.LIVE:
                    live_assets.add(asset)
                a = engine.books.get(pair.token_a)
                b = engine.books.get(pair.token_b)
                bid_a = a.best_bid() if a else None
                ask_a = a.best_ask() if a else None
                bid_b = b.best_bid() if b else None
                ask_b = b.best_ask() if b else None
                ask_pair = ask_a + ask_b if ask_a is not None and ask_b is not None else None
                bid_pair = bid_a + bid_b if bid_a is not None and bid_b is not None else None
                raw_edge = Decimal("1") - ask_pair if ask_pair is not None else None
                maker_edge = Decimal("1") - bid_pair if bid_pair is not None else None
                net_edge: Decimal | None = None
                quote_a = a.quote_buy(self.settings.min_trade_shares) if a else None
                quote_b = b.quote_buy(self.settings.min_trade_shares) if b else None
                if quote_a and quote_b:
                    fees = taker_fee(quote_a.segments, self.settings.crypto_taker_fee_rate) + taker_fee(
                        quote_b.segments, self.settings.crypto_taker_fee_rate
                    )
                    net_edge = (
                        self.settings.min_trade_shares - quote_a.notional - quote_b.notional - fees - self.settings.min_trade_shares * self.settings.risk_buffer_per_share
                    ) / self.settings.min_trade_shares
                tracker = edge_tracker.summary(pair.slug) if edge_tracker is not None else None
                surge = research.regime.current(pair.market_id) if research is not None else None
                window = updown_15m_window_from_slug(pair.slug)
                markets.append(
                    {
                        "asset": asset,
                        "phase": phase.value,
                        "slug": pair.slug,
                        "window": f"{window[0]:%H:%M}-{window[1]:%H:%M}Z" if window else None,
                        "outcome_a": pair.outcome_a,
                        "outcome_b": pair.outcome_b,
                        "bid_a": _fmt_decimal(bid_a),
                        "ask_a": _fmt_decimal(ask_a),
                        "bid_b": _fmt_decimal(bid_b),
                        "ask_b": _fmt_decimal(ask_b),
                        "ask_depth_a": _fmt_decimal(a.asks.get(ask_a, ZERO)) if a and ask_a is not None else None,
                        "ask_depth_b": _fmt_decimal(b.asks.get(ask_b, ZERO)) if b and ask_b is not None else None,
                        "ask_pair": _fmt_decimal(ask_pair),
                        "bid_pair": _fmt_decimal(bid_pair),
                        "raw_edge": _fmt_decimal(raw_edge),
                        "net_edge_5sh": _fmt_decimal(net_edge),
                        "maker_edge": _fmt_decimal(maker_edge),
                        "best_pair": _fmt_decimal(tracker.best_pair_price) if tracker else None,
                        "best_net_edge": _fmt_decimal(tracker.best_net_edge) if tracker else None,
                        "observations": tracker.observations if tracker else 0,
                        "surge": bool(surge.active) if surge else False,
                        "surge_reasons": list(surge.reasons) if surge else [],
                    }
                )

            strategies = self._strategy_rows(taker, research, hedge, frontier, dual_fok, split_sell)
            aggregate = sum(self._pnl_by_strategy.values(), ZERO)
            families: dict[str, dict[str, Any]] = {}
            for row in strategies:
                family = row["family"]
                info = families.setdefault(family, {"family": family, "pnl": 0.0, "models": 0, "wins": 0, "losses": 0})
                info["pnl"] += row["equity"]
                info["models"] += 1
                info["wins"] += row["wins"]
                info["losses"] += row["losses"]

            assets: list[dict[str, Any]] = []
            for asset in self.settings.market_assets:
                asset_markets = [market for market in markets if market["asset"] == asset]
                live = next((market for market in asset_markets if market["phase"] == "LIVE"), None)
                nxt = next((market for market in asset_markets if market["phase"] == "NEXT"), None)
                assets.append(
                    {
                        "asset": asset,
                        "pnl": float(self._pnl_by_asset.get(asset, ZERO)),
                        "live": live,
                        "next": nxt,
                        "status": "LIVE" if live else ("NEXT" if nxt else "SEARCHING"),
                    }
                )

            best_current_edge = max((market["net_edge_5sh"] for market in markets if market["net_edge_5sh"] is not None), default=None)
            self._state = {
                "generated_at": _utc_now(),
                "mode": "SHADOW_ONLY",
                "aggregate_shadow_pnl": float(aggregate),
                "aggregate_note": "sum of independent counterfactual shadow models; not one executable account",
                "profitable_models": sum(1 for row in strategies if row["equity"] > 0),
                "losing_models": sum(1 for row in strategies if row["equity"] < 0),
                "flat_models": sum(1 for row in strategies if row["equity"] == 0),
                "best_current_net_edge": best_current_edge,
                "assets": assets,
                "markets": sorted(markets, key=lambda market: (self.settings.market_assets.index(market["asset"]) if market["asset"] in self.settings.market_assets else 999, 0 if market["phase"] == "LIVE" else 1)),
                "strategies": strategies,
                "families": sorted(families.values(), key=lambda item: item["pnl"], reverse=True),
                "events": list(self._events),
                "feed": {
                    "messages": message_count,
                    "messages_per_second": self._message_rate,
                    "books_ready": sum(1 for book in engine.books.values() if book.ready),
                    "books_total": len(engine.books),
                    "live_assets": len(live_assets),
                    "configured_assets": len(self.settings.market_assets),
                },
            }
            state = _jsonable(self._state)

        path = Path(self.settings.dashboard_state_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            log.debug("Could not write dashboard state snapshot: %s", exc)
        return state

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return _jsonable(self._state)


INDEX_HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>ARB//TERM</title>
<style>
:root{--bg:#030705;--panel:#06100b;--line:#153c27;--green:#62ff9b;--green2:#b8ff67;--dim:#4a8c65;--red:#ff4f69;--amber:#ffd166;--cyan:#64ffd9;--shadow:0 0 18px rgba(98,255,155,.08)}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--green);font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,"Liberation Mono",monospace;font-size:13px}body{min-height:100vh;overflow-x:hidden;background-image:linear-gradient(rgba(0,255,128,.018) 1px,transparent 1px),linear-gradient(90deg,rgba(0,255,128,.018) 1px,transparent 1px);background-size:28px 28px}.crt:before{content:"";position:fixed;inset:0;pointer-events:none;z-index:99;background:repeating-linear-gradient(0deg,rgba(255,255,255,.018),rgba(255,255,255,.018) 1px,transparent 1px,transparent 3px);mix-blend-mode:screen}.scan{position:fixed;left:0;right:0;height:100px;top:-120px;background:linear-gradient(transparent,rgba(99,255,155,.035),transparent);animation:scan 8s linear infinite;pointer-events:none;z-index:98}@keyframes scan{to{transform:translateY(calc(100vh + 240px))}}.shell{max-width:1700px;margin:0 auto;padding:16px}.topbar{display:flex;align-items:center;gap:16px;border:1px solid var(--line);background:rgba(4,15,9,.94);padding:12px 14px;box-shadow:var(--shadow);position:sticky;top:0;z-index:20}.brand{font-size:22px;font-weight:900;letter-spacing:2px;color:var(--green2);text-shadow:0 0 14px rgba(184,255,103,.35)}.brand:after{content:"_";animation:blink 1s steps(1) infinite}@keyframes blink{50%{opacity:0}}.pill{border:1px solid var(--line);padding:5px 8px;color:var(--dim);white-space:nowrap}.pill.live{color:var(--green);border-color:#2b8050}.pill.warn{color:var(--amber)}.spacer{flex:1}.clock{color:var(--green2)}.ticker{display:flex;gap:8px;overflow:auto;padding:10px 0 2px}.ticker button,.filters button{font:inherit;color:var(--green);background:#06100b;border:1px solid var(--line);padding:6px 10px;cursor:pointer}.ticker button:hover,.ticker button.active,.filters button:hover,.filters button.active{border-color:var(--green);box-shadow:0 0 12px rgba(98,255,155,.12);color:var(--green2)}.kpis{display:grid;grid-template-columns:repeat(5,minmax(160px,1fr));gap:10px;margin:12px 0}.kpi,.panel,.asset{border:1px solid var(--line);background:rgba(5,14,9,.92);box-shadow:var(--shadow)}.kpi{padding:14px;min-height:92px}.label{color:var(--dim);font-size:11px;letter-spacing:1.3px;text-transform:uppercase}.value{font-size:28px;font-weight:900;margin-top:8px;color:var(--green2);text-shadow:0 0 10px rgba(98,255,155,.15)}.sub{font-size:10px;color:var(--dim);margin-top:6px}.loss{color:var(--red)!important}.profit{color:var(--green2)!important}.amber{color:var(--amber)!important}.assets{display:grid;grid-template-columns:repeat(7,minmax(170px,1fr));gap:8px;margin:10px 0}.asset{padding:10px;position:relative;overflow:hidden}.asset:after{content:"";position:absolute;bottom:0;left:0;width:100%;height:1px;background:linear-gradient(90deg,transparent,var(--green),transparent);animation:slide 4s linear infinite}@keyframes slide{from{transform:translateX(-100%)}to{transform:translateX(100%)}}.asset-head{display:flex;justify-content:space-between;align-items:center}.asset-name{font-size:18px;font-weight:900}.asset-status{font-size:10px;color:var(--dim)}.quote{margin-top:10px;display:grid;grid-template-columns:1fr 1fr;gap:5px}.quote div{border-top:1px dotted #183925;padding-top:4px}.quote b{color:var(--green2)}.main{display:grid;grid-template-columns:minmax(700px,1.7fr) minmax(360px,.8fr);gap:10px;margin-top:10px}.panel{padding:10px;min-height:200px}.panel-title{display:flex;align-items:center;justify-content:space-between;margin-bottom:9px;color:var(--green2);font-weight:800;letter-spacing:1.2px}.filters{display:flex;gap:5px;flex-wrap:wrap}.filters button{padding:3px 7px;font-size:10px}.table-wrap{max-height:520px;overflow:auto;border-top:1px solid var(--line)}table{border-collapse:collapse;width:100%}th,td{padding:7px 6px;text-align:left;border-bottom:1px solid rgba(21,60,39,.6);white-space:nowrap}th{position:sticky;top:0;background:#07120c;color:var(--dim);font-size:10px;z-index:2}tr:hover td{background:rgba(98,255,155,.025)}.strategy{font-weight:700;color:var(--green2)}.state{font-size:9px;border:1px solid var(--line);padding:2px 5px}.events{height:520px;overflow:auto;display:flex;flex-direction:column;gap:6px}.event{padding:7px;border-left:2px solid var(--line);background:#040c08;animation:fade .35s ease}@keyframes fade{from{opacity:.2;transform:translateY(-4px)}}.event.MAJOR_LOSS,.event.GHOST_TOXIC{border-color:var(--red)}.event.MAJOR_WIN,.event.WIN,.event.GHOST_WIN{border-color:var(--green2)}.event.FLEETING_ARB{border-color:var(--amber)}.event-top{display:flex;justify-content:space-between;gap:8px}.event-kind{font-weight:900}.event-meta{color:var(--dim);font-size:10px;margin-top:4px}.markets{margin-top:10px}.market-grid{display:grid;grid-template-columns:repeat(2,minmax(300px,1fr));gap:8px}.market{border:1px solid rgba(21,60,39,.8);padding:9px;background:#040c08}.market-head{display:flex;justify-content:space-between;color:var(--green2);font-weight:800}.market-cols{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin-top:8px}.metric{border-top:1px dotted #183925;padding-top:4px}.metric span{display:block;color:var(--dim);font-size:9px}.chart{height:110px;width:100%;display:block;border-top:1px solid var(--line);margin-top:8px}.footer{display:flex;justify-content:space-between;color:var(--dim);font-size:10px;padding:10px 2px}.dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--green);box-shadow:0 0 9px var(--green);margin-right:6px;animation:pulse 1.3s ease-in-out infinite}@keyframes pulse{50%{opacity:.25}}@media(max-width:1200px){.kpis{grid-template-columns:repeat(3,1fr)}.assets{grid-template-columns:repeat(4,1fr)}.main{grid-template-columns:1fr}.events{height:360px}}@media(max-width:700px){.shell{padding:8px}.kpis{grid-template-columns:1fr 1fr}.assets{grid-template-columns:1fr 1fr}.market-grid{grid-template-columns:1fr}.topbar{flex-wrap:wrap}.value{font-size:22px}}
</style>
</head>
<body class="crt"><div class="scan"></div><div class="shell">
<header class="topbar"><div class="brand">ARB//TERM</div><span class="pill live"><span class="dot"></span>SHADOW LINK</span><span id="feed" class="pill">FEED -- msg/s</span><span id="books" class="pill">BOOKS --/--</span><div class="spacer"></div><span class="pill warn">COUNTERFACTUAL RESEARCH</span><span id="clock" class="clock"></span></header>
<div id="ticker" class="ticker"></div>
<section class="kpis"><div class="kpi"><div class="label">Shadow Net</div><div id="net" class="value">0.0000</div><div class="sub">Σ independent model P&L · not executable account equity</div></div><div class="kpi"><div class="label">Models + / −</div><div id="models" class="value">0 / 0</div><div class="sub">profitable vs losing strategy variants</div></div><div class="kpi"><div class="label">Live Assets</div><div id="liveAssets" class="value">0 / 0</div><div class="sub">LIVE 15m markets currently subscribed</div></div><div class="kpi"><div class="label">Best Current Net Edge</div><div id="bestEdge" class="value">--</div><div class="sub">5-share fee+risk adjusted pair edge</div></div><div class="kpi"><div class="label">P&L Pulse</div><canvas id="pnlChart" class="chart"></canvas></div></section>
<section id="assets" class="assets"></section>
<div class="main"><section class="panel"><div class="panel-title"><span>MODEL MATRIX</span><div id="familyFilters" class="filters"></div></div><div class="table-wrap"><table><thead><tr><th>MODEL</th><th>STATE</th><th>P&L</th><th>W/L</th><th>PLACED</th><th>DONE</th><th>MISS</th><th>P(BOTH)</th><th>EV/PLACE</th><th>LIFE p50</th></tr></thead><tbody id="strategyRows"></tbody></table></div></section><section class="panel"><div class="panel-title"><span>EVENT TAPE</span><span class="label">wins · major losses · toxic fills · arb windows</span></div><div id="events" class="events"></div></section></div>
<section class="panel markets"><div class="panel-title"><span>LIVE / NEXT MARKET MATRIX</span><span id="generated" class="label"></span></div><div id="marketGrid" class="market-grid"></div></section>
<footer class="footer"><span>ARB//TERM v1.7 · observer/shadow only · no signing / wallet / live orders</span><span id="note"></span></footer>
</div>
<script>
let state=null, family='ALL', asset='ALL', history=[];
const $=id=>document.getElementById(id); const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));
function f(v,d=4){return v==null||Number.isNaN(Number(v))?'--':Number(v).toFixed(d)} function pct(v){return v==null?'--':(Number(v)*100).toFixed(1)+'%'} function pnlClass(v){return Number(v)>0?'profit':Number(v)<0?'loss':''}
function setText(id,text,cls=''){let e=$(id);e.textContent=text;e.className='value '+cls}
function renderTicker(){let items=['ALL',...(state.assets||[]).map(a=>a.asset)];$('ticker').innerHTML=items.map(a=>`<button class="${asset===a?'active':''}" onclick="asset='${a}';render()">${a}</button>`).join('')}
function renderAssets(){let rows=(state.assets||[]).filter(a=>asset==='ALL'||a.asset===asset);$('assets').innerHTML=rows.map(a=>{let m=a.live||a.next||{};let p=a.pnl||0;return `<div class="asset"><div class="asset-head"><span class="asset-name">${esc(a.asset)}</span><span class="asset-status ${a.status==='LIVE'?'profit':''}">${esc(a.status)}</span></div><div class="quote"><div><span class="label">UP</span><br><b>${f(m.bid_a,2)}/${f(m.ask_a,2)}</b></div><div><span class="label">DOWN</span><br><b>${f(m.bid_b,2)}/${f(m.ask_b,2)}</b></div><div><span class="label">PAIR</span><br>${f(m.ask_pair,3)}</div><div><span class="label">NET 5sh</span><br><span class="${pnlClass(m.net_edge_5sh)}">${m.net_edge_5sh==null?'--':(m.net_edge_5sh>=0?'+':'')+f(m.net_edge_5sh,4)}</span></div></div><div class="sub">SHADOW P&L <span class="${pnlClass(p)}">${p>=0?'+':''}${f(p,4)}</span> · ${esc(m.window||'awaiting market')}</div></div>`}).join('')}
function renderFilters(){let fams=['ALL',...new Set((state.strategies||[]).map(s=>s.family))];$('familyFilters').innerHTML=fams.map(x=>`<button class="${family===x?'active':''}" onclick="family='${x}';renderStrategies()">${x}</button>`).join('')}
function renderStrategies(){let rows=(state.strategies||[]).filter(s=>family==='ALL'||s.family===family);$('strategyRows').innerHTML=rows.map(s=>`<tr><td class="strategy">${esc(s.strategy)}</td><td><span class="state ${s.state==='LOSS'?'loss':s.state==='PROFIT'?'profit':''}">${esc(s.state)}</span></td><td class="${pnlClass(s.equity)}">${s.equity>=0?'+':''}${f(s.equity,4)}</td><td>${s.wins}/${s.losses}</td><td>${s.placements}</td><td>${s.completed}</td><td>${s.misses}</td><td>${pct(s.p_both)}</td><td class="${pnlClass(s.ev_per_placement)}">${s.ev_per_placement>=0?'+':''}${f(s.ev_per_placement,5)}</td><td>${s.median_lifetime_ms==null?'--':f(s.median_lifetime_ms,0)+'ms'}</td></tr>`).join('')}
function renderEvents(){let rows=(state.events||[]).filter(e=>asset==='ALL'||e.asset===asset);$('events').innerHTML=rows.slice(0,80).map(e=>`<div class="event ${esc(e.kind)}"><div class="event-top"><span class="event-kind ${e.kind.includes('LOSS')||e.kind==='GHOST_TOXIC'?'loss':e.kind.includes('WIN')?'profit':e.kind==='FLEETING_ARB'?'amber':''}">${esc(e.kind)}</span><span>${e.pnl==null?'':`<b class="${pnlClass(e.pnl)}">${e.pnl>=0?'+':''}${f(e.pnl,4)} pUSD</b>`}</span></div><div>${esc(e.asset)} · ${esc(e.strategy)} · ${esc(e.status||'')}</div><div class="event-meta">${esc(e.action||'')} ${e.counterfactual?'· counterfactual':''}</div></div>`).join('')||'<div class="sub">No substantial events yet.</div>'}
function renderMarkets(){let rows=(state.markets||[]).filter(m=>asset==='ALL'||m.asset===asset);$('marketGrid').innerHTML=rows.map(m=>`<div class="market"><div class="market-head"><span>${esc(m.asset)} // ${esc(m.phase)}</span><span>${esc(m.window||'')}</span></div><div class="market-cols"><div class="metric"><span>UP BID/ASK</span>${f(m.bid_a,2)}/${f(m.ask_a,2)}</div><div class="metric"><span>DOWN BID/ASK</span>${f(m.bid_b,2)}/${f(m.ask_b,2)}</div><div class="metric"><span>ASK PAIR</span>${f(m.ask_pair,4)}</div><div class="metric"><span>NET EDGE</span><b class="${pnlClass(m.net_edge_5sh)}">${m.net_edge_5sh==null?'--':(m.net_edge_5sh>=0?'+':'')+f(m.net_edge_5sh,4)}</b></div><div class="metric"><span>BEST PAIR</span>${f(m.best_pair,4)}</div><div class="metric"><span>BEST NET</span><span class="${pnlClass(m.best_net_edge)}">${m.best_net_edge==null?'--':(m.best_net_edge>=0?'+':'')+f(m.best_net_edge,4)}</span></div><div class="metric"><span>OBS</span>${m.observations||0}</div><div class="metric"><span>SURGE</span><span class="${m.surge?'amber':''}">${m.surge?'ON':'off'}</span></div></div></div>`).join('')}
function drawChart(){let c=$('pnlChart'),ctx=c.getContext('2d'),r=c.getBoundingClientRect(),d=devicePixelRatio||1;c.width=r.width*d;c.height=r.height*d;ctx.scale(d,d);ctx.clearRect(0,0,r.width,r.height);if(history.length<2)return;let vals=history.map(x=>x.v),mn=Math.min(...vals,0),mx=Math.max(...vals,0),span=Math.max(.001,mx-mn);ctx.strokeStyle='#153c27';ctx.beginPath();let zy=r.height-(0-mn)/span*r.height;ctx.moveTo(0,zy);ctx.lineTo(r.width,zy);ctx.stroke();ctx.strokeStyle=vals[vals.length-1]>=0?'#b8ff67':'#ff4f69';ctx.lineWidth=1.5;ctx.beginPath();history.forEach((p,i)=>{let x=i/(history.length-1)*r.width,y=r.height-(p.v-mn)/span*r.height;i?ctx.lineTo(x,y):ctx.moveTo(x,y)});ctx.stroke()}
function render(){if(!state)return;renderTicker();renderAssets();renderFilters();renderStrategies();renderEvents();renderMarkets();let n=Number(state.aggregate_shadow_pnl||0);setText('net',(n>=0?'+':'')+f(n,4)+' pUSD',pnlClass(n));setText('models',`${state.profitable_models||0} / ${state.losing_models||0}`);setText('liveAssets',`${state.feed?.live_assets||0} / ${state.feed?.configured_assets||0}`);let e=state.best_current_net_edge;setText('bestEdge',e==null?'--':(e>=0?'+':'')+f(e,4)+'/sh',pnlClass(e));$('feed').textContent=`FEED ${f(state.feed?.messages_per_second,0)} msg/s`;$('books').textContent=`BOOKS ${state.feed?.books_ready||0}/${state.feed?.books_total||0}`;$('generated').textContent='STATE '+(state.generated_at||'');$('note').textContent=state.aggregate_note||'';let last=history.at(-1);if(!last||last.v!==n){history.push({t:Date.now(),v:n});if(history.length>120)history.shift()}drawChart()}
async function poll(){try{let r=await fetch('/api/state',{cache:'no-store'});if(r.ok){state=await r.json();render()}}catch(e){}setTimeout(poll,500)}
setInterval(()=>{$('clock').textContent=new Date().toISOString().replace('T',' ').slice(0,19)+'Z'},250);window.addEventListener('resize',drawChart);poll();
</script></body></html>'''


class DashboardServer:
    def __init__(self, settings, state_getter: Callable[[], dict[str, Any]] | None = None) -> None:
        self.settings = settings
        self.state_getter = state_getter
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def _read_state_file(self) -> dict[str, Any]:
        path = Path(self.settings.dashboard_state_path)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"generated_at": _utc_now(), "mode": "SHADOW_ONLY", "assets": [], "markets": [], "strategies": [], "events": [], "feed": {}}

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
                    self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
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

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None


def ui_cli() -> None:
    from dotenv import load_dotenv
    from .config import Settings

    load_dotenv()
    settings = Settings()
    server = DashboardServer(settings)
    server.start()
    if server._server is None:
        raise SystemExit(1)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
