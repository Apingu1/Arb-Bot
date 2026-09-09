from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any

from .report_v183 import _iter_rows, _latest_run_id
from .report_v184_cli import cli as cli_v184
from .report_v181 import _asset, _d


PFOK_PREFIX = "PFOK"
EPISODE_WINDOW_MS = 500


def _med(values: list[Decimal]) -> Decimal:
    return Decimal(str(median(values))) if values else Decimal("0")


def _matches(payload: dict[str, Any], *, run_id: str | None, asset_filter: str | None) -> bool:
    if run_id and str(payload.get("phase183_run_id") or "") != run_id:
        return False
    if asset_filter and _asset(payload) != asset_filter:
        return False
    return True


def _timestamp(value: Any) -> float | None:
    if not value:
        return None
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _collect_pfok(path: Path, *, run_id: str | None, asset_filter: str | None):
    by_strategy: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: {"candidates": [], "rejects": [], "placements": [], "executions": []}
    )
    for row in _iter_rows(path) or ():
        payload = row["payload"]
        strategy = str(payload.get("strategy") or "")
        if not strategy.startswith(PFOK_PREFIX):
            continue
        if not _matches(payload, run_id=run_id, asset_filter=asset_filter):
            continue
        event_type = str(row.get("event_type") or "")
        if event_type == "profit_fok_candidate_v185":
            by_strategy[strategy]["candidates"].append(payload)
        elif event_type == "profit_fok_preflight_reject_v185":
            by_strategy[strategy]["rejects"].append(payload)
        elif event_type == "dual_fok_attempt_placed" and payload.get("mode") == "PROFIT_FOK_V185":
            by_strategy[strategy]["placements"].append(payload)
        elif event_type == "dual_fok_execution_summary" and payload.get("mode") == "PROFIT_FOK_V185":
            by_strategy[strategy]["executions"].append(payload)
    return by_strategy


def _profit_fok_table(path: Path, *, run_id: str | None, asset_filter: str | None) -> str:
    grouped = _collect_pfok(path, run_id=run_id, asset_filter=asset_filter)
    rows = grouped.get("PFOK", {"candidates": [], "rejects": [], "placements": [], "executions": []})
    candidates = rows["candidates"]
    rejects = rows["rejects"]
    placements = rows["placements"]
    executions = rows["executions"]

    lines = [
        "PHASE 1.8.5 PROFIT-FIRST PFOK CONTROL",
        "Original profitable PFOK is unchanged. Preflight rejects are NO-TRADE decisions and never count as wins or P&L.",
    ]
    if not candidates and not executions:
        lines.append("no Phase 1.8.5 PFOK control candidates yet")
        return "\n".join(lines)

    wins = [row for row in executions if _d(row.get("realized_pnl")) > 0]
    losses = [row for row in executions if _d(row.get("realized_pnl")) < 0]
    flats = len(executions) - len(wins) - len(losses)
    total_pnl = sum((_d(row.get("realized_pnl")) for row in executions), Decimal("0"))
    detected_edges = [_d(row.get("detected_edge_per_share")) for row in executions]
    preflight_edges = [
        _d(row.get("preflight_edge_per_share"))
        for row in executions
        if row.get("preflight_edge_per_share") is not None
    ]
    a_arrivals = [
        _d(row.get("actual_arrival_a_ms"))
        for row in executions
        if row.get("actual_arrival_a_ms") is not None
    ]
    b_arrivals = [
        _d(row.get("actual_arrival_b_ms"))
        for row in executions
        if row.get("actual_arrival_b_ms") is not None
    ]

    decided = len(executions)
    lines.extend(
        [
            f"candidates={len(candidates)} preflight_rejects={len(rejects)} submitted={len(placements)} finalized={decided}",
            f"wins={len(wins)} losses={len(losses)} flats={flats} win_rate={(len(wins)/decided*100 if decided else 0):.1f}% total_pnl={float(total_pnl):+.5f} pUSD avg_pnl={(float(total_pnl/Decimal(decided)) if decided else 0):+.5f}",
            f"p50_detected_edge={float(_med(detected_edges)):+.5f}/sh p50_preflight_edge={float(_med(preflight_edges)):+.5f}/sh p50_arrival_A={float(_med(a_arrivals)):.2f}ms p50_arrival_B={float(_med(b_arrivals)):.2f}ms",
        ]
    )

    by_size = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": Decimal("0")})
    by_asset = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": Decimal("0")})
    statuses = defaultdict(int)
    actions = defaultdict(int)
    for row in executions:
        pnl = _d(row.get("realized_pnl"))
        size = str(row.get("shares") or "?")
        asset = _asset(row)
        by_size[size]["n"] += 1
        by_size[size]["wins"] += int(pnl > 0)
        by_size[size]["pnl"] += pnl
        by_asset[asset]["n"] += 1
        by_asset[asset]["wins"] += int(pnl > 0)
        by_asset[asset]["pnl"] += pnl
        statuses[str(row.get("status") or "UNKNOWN")] += 1
        actions[str(row.get("action") or "UNKNOWN")] += 1

    if by_size:
        lines.extend(["", f"{'SIZE':>6s} {'N':>5s} {'WINS':>5s} {'WIN%':>7s} {'PNL':>11s}", "-" * 39])
        for size, item in sorted(by_size.items(), key=lambda kv: Decimal(kv[0]) if kv[0] != "?" else Decimal("0")):
            lines.append(
                f"{size:>6s} {item['n']:5d} {item['wins']:5d} {item['wins']/item['n']*100:6.1f}% {float(item['pnl']):+11.5f}"
            )

    if by_asset:
        lines.extend(["", f"{'ASSET':>7s} {'N':>5s} {'WINS':>5s} {'WIN%':>7s} {'PNL':>11s}", "-" * 40])
        for asset, item in sorted(by_asset.items()):
            lines.append(
                f"{asset:>7s} {item['n']:5d} {item['wins']:5d} {item['wins']/item['n']*100:6.1f}% {float(item['pnl']):+11.5f}"
            )

    if statuses:
        lines.append("")
        lines.append("outcomes=" + ", ".join(f"{k}:{v}" for k, v in sorted(statuses.items())))
        lines.append("actions=" + ", ".join(f"{k}:{v}" for k, v in sorted(actions.items())))

    if rejects:
        reasons = defaultdict(int)
        for row in rejects:
            reasons[str(row.get("reason") or "UNKNOWN")] += 1
        lines.append("preflight_reject_reasons=" + ", ".join(
            f"{k}:{v}" for k, v in sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))
        ))

    return "\n".join(lines)


def _frontier_table(path: Path, *, run_id: str | None, asset_filter: str | None) -> str:
    grouped = _collect_pfok(path, run_id=run_id, asset_filter=asset_filter)
    lines = [
        "PHASE 1.8.5 PFOK FRONTIER COMPARISON",
        "Parallel alternatives share market data but maintain independent shadow equity. DO NOT sum variant P&L as one deployable portfolio.",
        f"{'STRATEGY':16s} {'CAND':>5s} {'SUB':>5s} {'DONE':>5s} {'W/L':>9s} {'WIN%':>7s} {'PNL':>11s} {'EV/TRADE':>10s} {'MISS':>5s}",
        "-" * 87,
    ]
    names = ["PFOK", "PFOK-EDGE3", "PFOK-DEPTH1", "PFOK-NOSURGE", "PFOK-AGGR", "PFOK-S10", "PFOK-S20"]
    seen = False
    for name in names:
        item = grouped.get(name)
        if item is None:
            continue
        seen = True
        executions = item["executions"]
        wins = sum(1 for row in executions if _d(row.get("realized_pnl")) > 0)
        losses = sum(1 for row in executions if _d(row.get("realized_pnl")) < 0)
        pnl = sum((_d(row.get("realized_pnl")) for row in executions), Decimal("0"))
        n = len(executions)
        miss = sum(1 for row in executions if str(row.get("status") or "") == "ONE_LEG_MISS")
        lines.append(
            f"{name:16s} {len(item['candidates']):5d} {len(item['placements']):5d} {n:5d} "
            f"{wins:3d}/{losses:<3d} {(wins/n*100 if n else 0):6.1f}% {float(pnl):+11.5f} "
            f"{(float(pnl/Decimal(n)) if n else 0):+10.5f} {miss:5d}"
        )
    if not seen:
        lines.append("no PFOK frontier events yet")
    return "\n".join(lines)


def _episode_table(path: Path, *, run_id: str | None, asset_filter: str | None) -> str:
    grouped = _collect_pfok(path, run_id=run_id, asset_filter=asset_filter)
    events: list[tuple[float, str, dict[str, Any]]] = []
    for strategy, item in grouped.items():
        for row in item["executions"]:
            ts = _timestamp(row.get("finalized_at"))
            if ts is not None:
                events.append((ts, strategy, row))
    events.sort(key=lambda item: item[0])

    episodes: list[dict[str, Any]] = []
    last_by_market: dict[str, tuple[float, int]] = {}
    for ts, strategy, row in events:
        market = str(row.get("market_id") or row.get("slug") or "UNKNOWN")
        previous = last_by_market.get(market)
        if previous is None or (ts - previous[0]) * 1000 > EPISODE_WINDOW_MS:
            episode = {"market": market, "first_ts": ts, "last_ts": ts, "rows": [(strategy, row)]}
            episodes.append(episode)
            idx = len(episodes) - 1
        else:
            idx = previous[1]
            episodes[idx]["last_ts"] = ts
            episodes[idx]["rows"].append((strategy, row))
        last_by_market[market] = (ts, idx)

    lines = [
        "PHASE 1.8.5 INDEPENDENT PFOK EPISODES",
        f"Executions within {EPISODE_WINDOW_MS} ms on the same market are one correlated episode, regardless of how many variants fired.",
    ]
    if not episodes:
        lines.append("no finalized PFOK episodes yet")
        return "\n".join(lines)

    control_episodes = 0
    any_positive = 0
    any_loss = 0
    for episode in episodes:
        rows = episode["rows"]
        if any(strategy == "PFOK" for strategy, _ in rows):
            control_episodes += 1
        pnls = [_d(row.get("realized_pnl")) for _, row in rows]
        any_positive += int(any(p > 0 for p in pnls))
        any_loss += int(any(p < 0 for p in pnls))
    lines.append(
        f"independent_episodes={len(episodes)} control_participated={control_episodes} episodes_with_any_win={any_positive} episodes_with_any_loss={any_loss}"
    )
    return "\n".join(lines)


def _atomic_pfok_funnel(path: Path, *, run_id: str | None, asset_filter: str | None) -> str:
    atomic: list[tuple[str, float]] = []
    pfok_candidates: list[tuple[str, float]] = []
    pfok_exec: list[tuple[str, float, Decimal]] = []

    for row in _iter_rows(path) or ():
        payload = row["payload"]
        if not _matches(payload, run_id=run_id, asset_filter=asset_filter):
            continue
        event_type = str(row.get("event_type") or "")
        strategy = str(payload.get("strategy") or "")
        market = str(payload.get("market_id") or payload.get("slug") or "")
        if event_type == "atomic_benchmark_capture_v183" and strategy == "ATOMIC-BUY-S5":
            ts = _timestamp(payload.get("captured_at"))
            if ts is not None:
                atomic.append((market, ts))
        elif event_type == "profit_fok_candidate_v185" and strategy == "PFOK":
            ts = _timestamp(payload.get("detected_at"))
            if ts is not None:
                pfok_candidates.append((market, ts))
        elif event_type == "dual_fok_execution_summary" and strategy == "PFOK" and payload.get("mode") == "PROFIT_FOK_V185":
            ts = _timestamp(payload.get("finalized_at"))
            if ts is not None:
                pfok_exec.append((market, ts, _d(payload.get("realized_pnl"))))

    lines = [
        "PHASE 1.8.5 ATOMIC -> PFOK CONTROL FUNNEL",
        "Uses ATOMIC-BUY-S5 as the comparable ideal 5-share window. Matching is by market and nearby timestamp; atomic remains benchmark-only.",
    ]
    if not atomic:
        lines.append("no comparable ATOMIC-BUY-S5 captures yet")
        return "\n".join(lines)

    match_ms = 500
    candidate_matches = 0
    finalized_matches = 0
    winning_matches = 0
    for market, ats in atomic:
        has_candidate = any(market == m and abs(ts - ats) * 1000 <= match_ms for m, ts in pfok_candidates)
        has_exec = [(ts, pnl) for m, ts, pnl in pfok_exec if market == m and 0 <= (ts - ats) * 1000 <= 2000]
        candidate_matches += int(has_candidate)
        finalized_matches += int(bool(has_exec))
        winning_matches += int(any(pnl > 0 for _, pnl in has_exec))

    lines.append(
        f"atomic_s5_windows={len(atomic)} matched_control_candidates={candidate_matches} matched_control_finalized={finalized_matches} matched_control_wins={winning_matches} unmatched_atomic={len(atomic)-candidate_matches}"
    )
    return "\n".join(lines)


def cli() -> None:
    cli_v184()

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("path", nargs="?", default="data/shadow_events.jsonl")
    parser.add_argument("--asset", default=None)
    parser.add_argument("--session", nargs="?", const="latest", default=None)
    args, _ = parser.parse_known_args()

    path = Path(args.path)
    run_id = None
    if args.session:
        run_id = _latest_run_id(path) if args.session == "latest" else args.session
    asset_filter = args.asset.upper() if args.asset else None

    print()
    print(_profit_fok_table(path, run_id=run_id, asset_filter=asset_filter))
    print()
    print(_frontier_table(path, run_id=run_id, asset_filter=asset_filter))
    print()
    print(_episode_table(path, run_id=run_id, asset_filter=asset_filter))
    print()
    print(_atomic_pfok_funnel(path, run_id=run_id, asset_filter=asset_filter))


if __name__ == "__main__":
    cli()
