from __future__ import annotations

import argparse
import os
import sys
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path
from statistics import median

from .report_v181 import _asset, _d
from .report_v183 import _iter_rows
from .report_v185_diag_cli import cli as cli_v185_diag
from .report_v186_cli import _atomic_frontier, _fast_session_rewrite


BFOK_NAMES = ["BFOK-1", "BFOK-5", "BFOK-10", "BFOK-20", "BFOK-EV"]
CORE_NAMES = ["PFOK", "PFOK-S10", "PFOK-S20", *BFOK_NAMES]


def _med(values: list[float]) -> float:
    return float(median(values)) if values else 0.0


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * q))))
    return float(ordered[idx])


def _bfok_table(path: Path, asset_filter: str | None) -> str:
    stats = defaultdict(lambda: {"cand": 0, "sub": 0, "exec": []})
    for row in _iter_rows(path) or ():
        p = row.get("payload", {})
        if asset_filter and _asset(p) != asset_filter:
            continue
        name = str(p.get("strategy") or "")
        if not name.startswith("BFOK-"):
            continue
        et = str(row.get("event_type") or "")
        if et == "batch_fok_candidate_v187":
            stats[name]["cand"] += 1
        elif et == "batch_fok_submission_v187":
            stats[name]["sub"] += 1
        elif et == "batch_fok_execution_summary_v187":
            stats[name]["exec"].append(p)

    lines = [
        "PHASE 1.8.7 PARALLEL BATCH-FOK",
        "Two FOK legs share one modeled batch-arrival deadline but are independently validated; one-leg misses and recovery losses remain real shadow P&L.",
        f"{'STRATEGY':12s} {'CAND':>5s} {'SUB':>5s} {'DONE':>5s} {'BOTH':>5s} {'MISS':>5s} {'NONE':>5s} {'P(BOTH)':>8s} {'W/L':>9s} {'PNL':>11s} {'EV/SUB':>10s}",
        "-" * 103,
    ]
    any_rows = False
    for name in BFOK_NAMES:
        s = stats.get(name)
        if not s:
            continue
        any_rows = True
        executions = s["exec"]
        both = sum(1 for p in executions if p.get("status") == "BOTH_FILLED")
        miss = sum(1 for p in executions if p.get("status") == "ONE_LEG_MISS")
        none = sum(1 for p in executions if p.get("status") == "NEITHER_FILLED")
        wins = sum(1 for p in executions if _d(p.get("realized_pnl")) > 0)
        losses = sum(1 for p in executions if _d(p.get("realized_pnl")) < 0)
        pnl = sum((_d(p.get("realized_pnl")) for p in executions), Decimal("0"))
        done = len(executions)
        p_both = both / done * 100 if done else 0.0
        ev = pnl / Decimal(s["sub"]) if s["sub"] else Decimal("0")
        lines.append(
            f"{name:12s} {s['cand']:5d} {s['sub']:5d} {done:5d} {both:5d} {miss:5d} {none:5d} "
            f"{p_both:7.1f}% {wins:3d}/{losses:<3d} {float(pnl):+11.5f} {float(ev):+10.5f}"
        )
    if not any_rows:
        lines.append("no Phase 1.8.7 BFOK candidates yet")
    return "\n".join(lines)


def _latency_table(path: Path, asset_filter: str | None) -> str:
    stats = defaultdict(lambda: {"actual": [], "slip": [], "both": 0, "miss": 0, "none": 0})
    for row in _iter_rows(path) or ():
        if row.get("event_type") != "batch_fok_latency_v187":
            continue
        p = row.get("payload", {})
        if asset_filter and _asset(p) != asset_filter:
            continue
        name = str(p.get("strategy") or "")
        s = stats[name]
        if p.get("actual_arrival_ms") is not None:
            s["actual"].append(float(p["actual_arrival_ms"]))
        if p.get("scheduler_slippage_ms") is not None:
            s["slip"].append(float(p["scheduler_slippage_ms"]))
        a = bool(p.get("leg_a_filled"))
        b = bool(p.get("leg_b_filled"))
        if a and b:
            s["both"] += 1
        elif a or b:
            s["miss"] += 1
        else:
            s["none"] += 1

    lines = [
        "PHASE 1.8.7 BFOK ARRIVAL LATENCY",
        "Both orders have the same target arrival. Scheduler slippage is local shadow-runtime lateness, not exchange acknowledgement latency.",
        f"{'STRATEGY':12s} {'P50 ARR':>9s} {'P95 ARR':>9s} {'P50 SLIP':>10s} {'P95 SLIP':>10s} {'BOTH':>5s} {'MISS':>5s} {'NONE':>5s}",
        "-" * 78,
    ]
    if not stats:
        lines.append("no BFOK arrival samples yet")
        return "\n".join(lines)
    for name in BFOK_NAMES:
        s = stats.get(name)
        if not s:
            continue
        lines.append(
            f"{name:12s} {_med(s['actual']):8.2f}ms {_pct(s['actual'], .95):8.2f}ms "
            f"{_med(s['slip']):9.2f}ms {_pct(s['slip'], .95):9.2f}ms {s['both']:5d} {s['miss']:5d} {s['none']:5d}"
        )
    return "\n".join(lines)


def _ev_table(path: Path, asset_filter: str | None) -> str:
    by_size = defaultdict(lambda: {"checks": 0, "pass": 0, "expected": [], "pboth": [], "pmiss": [], "samples": []})
    for row in _iter_rows(path) or ():
        if row.get("event_type") != "batch_fok_ev_gate_v187":
            continue
        p = row.get("payload", {})
        if asset_filter and _asset(p) != asset_filter:
            continue
        shares = str(p.get("shares") or "?")
        s = by_size[shares]
        s["checks"] += 1
        s["pass"] += 1 if p.get("passed") else 0
        for key, target in (("expected_pnl", "expected"), ("p_both", "pboth"), ("p_miss", "pmiss"), ("samples", "samples")):
            if p.get(key) is not None:
                s[target].append(float(p[key]))

    lines = [
        "PHASE 1.8.7 BFOK-EV RISK GATE",
        "BFOK-EV uses smoothed size × asset × edge-band outcomes from fixed BFOK controls. Fixed controls still trade independently to keep learning unbiased by the EV gate.",
        f"{'SIZE':>6s} {'CHECKS':>7s} {'PASS':>6s} {'PASS%':>7s} {'P50 E[P&L]':>12s} {'P50 P(BOTH)':>12s} {'P50 P(MISS)':>12s} {'P50 N':>7s}",
        "-" * 82,
    ]
    if not by_size:
        lines.append("no BFOK-EV gate observations yet")
        return "\n".join(lines)
    for shares in sorted(by_size, key=lambda x: float(x) if x != "?" else 999999):
        s = by_size[shares]
        rate = s["pass"] / s["checks"] * 100 if s["checks"] else 0
        lines.append(
            f"{shares:>6s} {s['checks']:7d} {s['pass']:6d} {rate:6.1f}% { _med(s['expected']):+12.5f} "
            f"{_med(s['pboth'])*100:11.1f}% {_med(s['pmiss'])*100:11.1f}% {_med(s['samples']):7.1f}"
        )
    return "\n".join(lines)


def _economics_table(path: Path, asset_filter: str | None) -> str:
    stats = defaultdict(lambda: {"wins": [], "miss_losses": [], "all": []})
    for row in _iter_rows(path) or ():
        et = row.get("event_type")
        p = row.get("payload", {})
        name = str(p.get("strategy") or "")
        if name not in CORE_NAMES:
            continue
        if asset_filter and _asset(p) != asset_filter:
            continue
        if et == "batch_fok_execution_summary_v187" or (et == "dual_fok_execution_summary" and name.startswith("PFOK")):
            pnl = _d(p.get("realized_pnl"))
            status = str(p.get("status") or "")
            stats[name]["all"].append(pnl)
            if status == "BOTH_FILLED" and pnl > ZERO:
                stats[name]["wins"].append(pnl)
            if status == "ONE_LEG_MISS" and pnl < ZERO:
                stats[name]["miss_losses"].append(-pnl)

    lines = [
        "PHASE 1.8.7 WIN VS MISS ECONOMICS",
        "Break-even P(BOTH) uses average positive complete-set win versus average one-leg loss: L/(W+L). It is descriptive, not a guarantee.",
        f"{'STRATEGY':12s} {'TRADES':>6s} {'AVG WIN':>11s} {'AVG MISS LOSS':>14s} {'BE P(BOTH)':>11s} {'NET PNL':>11s}",
        "-" * 72,
    ]
    if not stats:
        lines.append("no core PFOK/BFOK executions yet")
        return "\n".join(lines)
    for name in CORE_NAMES:
        s = stats.get(name)
        if not s:
            continue
        avg_win = sum(s["wins"], Decimal("0")) / Decimal(len(s["wins"])) if s["wins"] else Decimal("0")
        avg_loss = sum(s["miss_losses"], Decimal("0")) / Decimal(len(s["miss_losses"])) if s["miss_losses"] else Decimal("0")
        denom = avg_win + avg_loss
        be = avg_loss / denom * Decimal("100") if denom > ZERO else Decimal("0")
        pnl = sum(s["all"], Decimal("0"))
        lines.append(
            f"{name:12s} {len(s['all']):6d} {float(avg_win):+11.5f} {float(avg_loss):14.5f} {float(be):10.1f}% {float(pnl):+11.5f}"
        )
    return "\n".join(lines)


def _episode_table(path: Path, asset_filter: str | None) -> str:
    from datetime import datetime

    window_ms = int(os.getenv("V185_EPISODE_WINDOW_MS", "500"))
    by_key = defaultdict(list)
    for row in _iter_rows(path) or ():
        et = row.get("event_type")
        p = row.get("payload", {})
        name = str(p.get("strategy") or "")
        if name not in CORE_NAMES:
            continue
        if asset_filter and _asset(p) != asset_filter:
            continue
        if et not in {"dual_fok_execution_summary", "batch_fok_execution_summary_v187"}:
            continue
        text = p.get("finalized_at")
        if not text:
            continue
        try:
            ts = datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        market = str(p.get("market_id") or p.get("slug") or "UNKNOWN")
        by_key[(name, market)].append((ts, _d(p.get("realized_pnl"))))

    stats = defaultdict(lambda: {"raw": Decimal("0"), "lower": Decimal("0"), "fills": 0, "episodes": 0})
    for (name, _market), rows in by_key.items():
        rows.sort()
        last = None
        for ts, pnl in rows:
            s = stats[name]
            s["raw"] += pnl
            s["fills"] += 1
            if last is None or (ts - last) * 1000 > window_ms:
                s["episodes"] += 1
                s["lower"] += pnl
            last = ts

    lines = [
        "PHASE 1.8.7 EPISODE-FIRST-FILL P&L",
        f"Conservative capacity proxy: repeated fills <= {window_ms} ms apart on one market count once. This still is not a full exchange-liquidity replenishment ledger.",
        f"{'STRATEGY':12s} {'FILLS':>6s} {'EPISODES':>8s} {'RAW PNL':>11s} {'LOWER PNL':>12s}",
        "-" * 56,
    ]
    if not stats:
        lines.append("no PFOK/BFOK executions yet")
        return "\n".join(lines)
    for name in CORE_NAMES:
        s = stats.get(name)
        if not s:
            continue
        lines.append(
            f"{name:12s} {s['fills']:6d} {s['episodes']:8d} {float(s['raw']):+11.5f} {float(s['lower']):+12.5f}"
        )
    return "\n".join(lines)


def cli() -> None:
    original = list(sys.argv)
    rewritten, sidecar = _fast_session_rewrite(original[1:])
    try:
        sys.argv = [original[0], *rewritten]
        cli_v185_diag()
    finally:
        sys.argv = original

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("path", nargs="?", default="data/shadow_events.jsonl")
    parser.add_argument("--asset", default=None)
    parser.add_argument("--session", nargs="?", const="latest", default=None)
    args, _ = parser.parse_known_args(original[1:])
    path = sidecar if sidecar is not None else Path(args.path)
    asset_filter = args.asset.upper() if args.asset else None

    print()
    print(_bfok_table(path, asset_filter))
    print()
    print(_latency_table(path, asset_filter))
    print()
    print(_ev_table(path, asset_filter))
    print()
    print(_economics_table(path, asset_filter))
    print()
    print(_atomic_frontier(path, asset_filter))
    print()
    print(_episode_table(path, asset_filter))


if __name__ == "__main__":
    cli()
