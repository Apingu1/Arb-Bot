from __future__ import annotations

import argparse
import os
import sys
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any

from .report_v181 import _asset, _d
from .report_v183 import _iter_rows
from .report_v185_diag_cli import cli as cli_v185_diag


FAST_MODE = "PROFIT_FOK_V186_FAST"
FAST_NAMES = [
    "PFOK-FAST1",
    "PFOK-FAST2",
    "PFOK-REQUOTE1",
    "PFOK-REQUOTE2",
    "PFOK-FAST-S10",
    "PFOK-FAST-S20",
]


def _med(values: list[float]) -> float:
    return float(median(values)) if values else 0.0


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * q))))
    return float(ordered[idx])


def _fast_session_rewrite(argv: list[str]) -> tuple[list[str], Path | None]:
    if "--session" not in argv:
        return argv, None
    session_path = Path(os.getenv("V186_CURRENT_SESSION_PATH", "data/current_session_v186.jsonl"))
    if not session_path.exists():
        return argv, None

    rewritten: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--session":
            # argparse's optional value is commonly omitted. Remove a following
            # explicit "latest" or run id because the sidecar is already one run.
            if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                nxt = argv[i + 1]
                if nxt == "latest" or (len(nxt) == 12 and all(c in "0123456789abcdef" for c in nxt.lower())):
                    i += 2
                    continue
            i += 1
            continue
        rewritten.append(arg)
        i += 1

    # If no explicit JSONL positional path is present, use the current-session sidecar.
    has_path = any(not arg.startswith("-") and arg.endswith(".jsonl") for arg in rewritten)
    if not has_path:
        rewritten.insert(0, str(session_path))
    return rewritten, session_path


def _fast_table(path: Path, asset_filter: str | None) -> str:
    stats = defaultdict(lambda: {"cand": 0, "reject": 0, "placed": 0, "exec": []})
    for row in _iter_rows(path) or ():
        p = row.get("payload", {})
        if p.get("mode") != FAST_MODE:
            continue
        if asset_filter and _asset(p) != asset_filter:
            continue
        name = str(p.get("strategy") or "")
        et = str(row.get("event_type") or "")
        if et == "profit_fok_candidate_v185":
            stats[name]["cand"] += 1
        elif et == "profit_fok_preflight_reject_v185":
            stats[name]["reject"] += 1
        elif et == "dual_fok_attempt_placed":
            stats[name]["placed"] += 1
        elif et == "dual_fok_execution_summary":
            stats[name]["exec"].append(p)

    lines = [
        "PHASE 1.8.6 LATENCY PFOK FRONTIER",
        "Fast variants use the same positive-edge economics as control PFOK. Variant P&L is alternative/counterfactual and must not be summed.",
        f"{'STRATEGY':18s} {'CAND':>5s} {'REJ':>5s} {'SUB':>5s} {'DONE':>5s} {'W/L':>9s} {'PNL':>11s} {'EV':>10s} {'MISS':>5s}",
        "-" * 92,
    ]
    any_rows = False
    for name in FAST_NAMES:
        s = stats.get(name)
        if s is None:
            continue
        any_rows = True
        executions = s["exec"]
        wins = sum(1 for p in executions if _d(p.get("realized_pnl")) > 0)
        losses = sum(1 for p in executions if _d(p.get("realized_pnl")) < 0)
        pnl = sum((_d(p.get("realized_pnl")) for p in executions), Decimal("0"))
        miss = sum(1 for p in executions if p.get("status") == "ONE_LEG_MISS")
        n = len(executions)
        lines.append(
            f"{name:18s} {s['cand']:5d} {s['reject']:5d} {s['placed']:5d} {n:5d} "
            f"{wins:3d}/{losses:<3d} {float(pnl):+11.5f} {(float(pnl/Decimal(n)) if n else 0):+10.5f} {miss:5d}"
        )
    if not any_rows:
        lines.append("no Phase 1.8.6 fast PFOK events yet")
    return "\n".join(lines)


def _latency_table(path: Path, asset_filter: str | None) -> str:
    by_strategy = defaultdict(lambda: defaultdict(list))
    outcomes = defaultdict(Counter)
    for row in _iter_rows(path) or ():
        if row.get("event_type") != "pfok_latency_v186":
            continue
        p = row.get("payload", {})
        if asset_filter and _asset(p) != asset_filter:
            continue
        name = str(p.get("strategy") or "")
        stage = str(p.get("stage") or "")
        if stage == "CANDIDATE":
            for key in ("book_update_ms", "snapshot_build_ms", "callback_to_candidate_ms"):
                if p.get(key) is not None:
                    by_strategy[name][key].append(float(p[key]))
        elif stage == "PREFLIGHT":
            for key in ("actual_preflight_latency_ms", "scheduler_slippage_ms"):
                if p.get(key) is not None:
                    by_strategy[name][key].append(float(p[key]))
            outcomes[name][str(p.get("outcome") or "UNKNOWN")] += 1
        elif stage == "SECOND_LEG":
            if p.get("actual_second_arrival_ms") is not None:
                by_strategy[name]["actual_second_arrival_ms"].append(float(p["actual_second_arrival_ms"]))
            if p.get("scheduler_slippage_ms") is not None:
                by_strategy[name]["second_slippage_ms"].append(float(p["scheduler_slippage_ms"]))

    lines = [
        "PHASE 1.8.6 LOCAL LATENCY / SCHEDULER",
        "Times are local process timings. Scheduler slippage = actual timer handling minus configured due time.",
        f"{'STRATEGY':18s} {'P50 PRE':>9s} {'P95 PRE':>9s} {'P50 SLIP':>10s} {'P95 SLIP':>10s} {'P50 2ND':>9s} {'P50 SNAP':>10s} {'PREFLIGHT':>16s}",
        "-" * 105,
    ]
    any_rows = False
    for name in FAST_NAMES:
        s = by_strategy.get(name)
        if not s:
            continue
        any_rows = True
        pre = s["actual_preflight_latency_ms"]
        slip = s["scheduler_slippage_ms"]
        second = s["actual_second_arrival_ms"]
        snap = s["snapshot_build_ms"]
        out = outcomes.get(name, {})
        out_text = "/".join(f"{k}:{v}" for k, v in sorted(out.items())) or "-"
        lines.append(
            f"{name:18s} {_med(pre):8.2f}ms {_pct(pre,.95):8.2f}ms {_med(slip):9.2f}ms {_pct(slip,.95):9.2f}ms "
            f"{_med(second):8.2f}ms {_med(snap):9.3f}ms {out_text:>16s}"
        )
    if not any_rows:
        lines.append("no Phase 1.8.6 latency samples yet")
    return "\n".join(lines)


def _atomic_frontier(path: Path, asset_filter: str | None) -> str:
    stats = defaultdict(lambda: {"n": 0, "fill": 0, "pnl": Decimal("0"), "out": Counter(), "elapsed": []})
    for row in _iter_rows(path) or ():
        if row.get("event_type") != "atomic_execution_proxy_v184":
            continue
        p = row.get("payload", {})
        strategy = str(p.get("strategy") or "")
        if not strategy.startswith("ATOMIC-BUY"):
            continue
        if asset_filter and _asset(p) != asset_filter:
            continue
        key = (strategy, int(p.get("target_latency_ms") or 0))
        item = stats[key]
        item["n"] += 1
        outcome = str(p.get("outcome") or "UNKNOWN")
        item["out"][outcome] += 1
        if p.get("actual_elapsed_ms") is not None:
            item["elapsed"].append(float(p["actual_elapsed_ms"]))
        if outcome == "EXECUTABLE_SHADOW_FILL":
            item["fill"] += 1
            item["pnl"] += _d(p.get("pnl"))

    lines = [
        "PHASE 1.8.6 ATOMIC EXECUTION FRONTIER",
        "Ideal captures are excluded here; this table shows the existing full-book re-quote execution proxy only.",
        f"{'STRATEGY':20s} {'LAT':>5s} {'N':>4s} {'FILL':>5s} {'RATE':>7s} {'PNL':>10s} {'P50 ACT':>10s}  OUTCOMES",
        "-" * 112,
    ]
    if not stats:
        lines.append("no atomic execution-proxy attempts yet")
        return "\n".join(lines)
    for (strategy, lat), s in sorted(stats.items()):
        rate = s["fill"] / s["n"] * 100 if s["n"] else 0
        lines.append(
            f"{strategy:20s} {lat:4d}ms {s['n']:4d} {s['fill']:5d} {rate:6.1f}% {float(s['pnl']):+10.5f} "
            f"{_med(s['elapsed']):9.2f}ms  {dict(s['out'])}"
        )
    return "\n".join(lines)


def _gate_rollups(path: Path, asset_filter: str | None) -> str:
    # Rollups are process-wide and omit asset to keep the hot path compact.
    totals = defaultdict(Counter)
    samples = Counter()
    for row in _iter_rows(path) or ():
        if row.get("event_type") != "profit_fok_gate_rollup_v186":
            continue
        p = row.get("payload", {})
        name = str(p.get("strategy") or "UNKNOWN")
        samples[name] += int(p.get("samples") or 0)
        totals[name].update(p.get("gate_reasons") or {})

    lines = [
        "PHASE 1.8.6 COMPACT PFOK GATE ROLLUPS",
        "Raw gate samples are aggregated in memory; counts remain exact while disk I/O is reduced.",
    ]
    if not samples:
        lines.append("no compact gate rollups yet")
        return "\n".join(lines)
    for name in ["PFOK", "PFOK-EDGE3", "PFOK-DEPTH1", "PFOK-NOSURGE", "PFOK-AGGR", "PFOK-S10", "PFOK-S20"]:
        if not samples[name]:
            continue
        reasons = ", ".join(f"{k}:{v}" for k, v in totals[name].most_common())
        lines.append(f"{name:16s} samples={samples[name]} | {reasons}")
    return "\n".join(lines)


def _episode_lower_bound(path: Path, asset_filter: str | None) -> str:
    # Conservative research proxy: within each strategy/market, executions less
    # than 500 ms apart are one episode and only the first P&L is counted. This
    # deliberately avoids claiming repeated displayed liquidity as independent.
    from datetime import datetime

    by_key = defaultdict(list)
    for row in _iter_rows(path) or ():
        if row.get("event_type") != "dual_fok_execution_summary":
            continue
        p = row.get("payload", {})
        name = str(p.get("strategy") or "")
        if not name.startswith("PFOK"):
            continue
        if asset_filter and _asset(p) != asset_filter:
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
            if last is None or (ts - last) * 1000 > 500:
                s["episodes"] += 1
                s["lower"] += pnl
            last = ts

    lines = [
        "PHASE 1.8.6 EPISODE-FIRST-FILL P&L LOWER BOUND",
        "Research-only conservative proxy: repeated fills <=500 ms apart on one market count once. It is not a full exchange-liquidity replenishment model.",
        f"{'STRATEGY':18s} {'FILLS':>6s} {'EPISODES':>8s} {'RAW PNL':>11s} {'LOWER PNL':>12s}",
        "-" * 62,
    ]
    if not stats:
        lines.append("no PFOK executions yet")
        return "\n".join(lines)
    order = ["PFOK", "PFOK-S10", "PFOK-S20", "PFOK-NOSURGE", "PFOK-DEPTH1", "PFOK-AGGR", "PFOK-EDGE3", *FAST_NAMES]
    for name in order:
        s = stats.get(name)
        if not s:
            continue
        lines.append(
            f"{name:18s} {s['fills']:6d} {s['episodes']:8d} {float(s['raw']):+11.5f} {float(s['lower']):+12.5f}"
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
    print(_fast_table(path, asset_filter))
    print()
    print(_latency_table(path, asset_filter))
    print()
    print(_atomic_frontier(path, asset_filter))
    print()
    print(_gate_rollups(path, asset_filter))
    print()
    print(_episode_lower_bound(path, asset_filter))


if __name__ == "__main__":
    cli()
