from __future__ import annotations

import argparse
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from statistics import median

from .report_v183 import _filter_event, _iter_rows, _latest_run_id
from .report_v184_cli import cli as cli_v184
from .report_v181 import _asset, _d


def _med(values: list[Decimal]) -> Decimal:
    return Decimal(str(median(values))) if values else Decimal("0")


def _profit_fok_table(
    path: Path,
    *,
    run_id: str | None,
    asset_filter: str | None,
) -> str:
    candidates = []
    rejects = []
    placements = []
    executions = []

    for row in _iter_rows(path) or ():
        event_type = str(row.get("event_type") or "")
        payload = row["payload"]
        if str(payload.get("strategy") or "") != "PFOK":
            continue
        if not _filter_event(payload, run_id=run_id, strategy="PFOK", asset=asset_filter):
            continue
        if event_type == "profit_fok_candidate_v185":
            candidates.append(payload)
        elif event_type == "profit_fok_preflight_reject_v185":
            rejects.append(payload)
        elif event_type == "dual_fok_attempt_placed" and payload.get("mode") == "PROFIT_FOK_V185":
            placements.append(payload)
        elif event_type == "dual_fok_execution_summary" and payload.get("mode") == "PROFIT_FOK_V185":
            executions.append(payload)

    lines = [
        "PHASE 1.8.5 PROFIT-FIRST PFOK",
        "Canonical BUY complete-set shadow strategy. Preflight rejects are NO-TRADE decisions and never count as wins or P&L.",
    ]
    if not candidates and not executions:
        lines.append("no Phase 1.8.5 PFOK candidates yet")
        return "\n".join(lines)

    wins = [row for row in executions if _d(row.get("realized_pnl")) > 0]
    losses = [row for row in executions if _d(row.get("realized_pnl")) < 0]
    flats = len(executions) - len(wins) - len(losses)
    total_pnl = sum((_d(row.get("realized_pnl")) for row in executions), Decimal("0"))
    pnls = [_d(row.get("realized_pnl")) for row in executions]
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

    submitted = len(placements)
    decided = len(executions)
    lines.extend(
        [
            f"candidates={len(candidates)} preflight_rejects={len(rejects)} submitted={submitted} finalized={decided}",
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
        lines.append("preflight_reject_reasons=" + ", ".join(f"{k}:{v}" for k, v in sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))))

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


if __name__ == "__main__":
    cli()
