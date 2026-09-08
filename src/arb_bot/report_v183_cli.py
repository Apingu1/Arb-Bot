from __future__ import annotations

import argparse
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from .report_v181 import SUMMARY_EVENT_TYPES, TRUE_COMPLETION_STATUSES, _asset, _d, _strategy
from .report_v183 import (
    _edge_bucket,
    _filter_event,
    _iter_rows,
    _latest_run_id,
    cli as cli_v183,
)


def _family(strategy: str) -> str | None:
    if strategy.startswith("SHYB-"):
        return "SHYB"
    if strategy.startswith("SPMAKER-"):
        return "SPMAKER"
    if strategy.startswith("SMAKER-"):
        return "SMAKER"
    return None


def _family_edge_table(
    path: Path,
    *,
    run_id: str | None,
    strategy_filter: str | None,
    asset_filter: str | None,
) -> str:
    groups = defaultdict(
        lambda: {
            "n": 0,
            "pnl": Decimal("0"),
            "wins": 0,
            "episodes": set(),
            "win_episodes": set(),
            "legacy_win_episodes": 0,
        }
    )
    for row in _iter_rows(path) or ():
        event_type = str(row.get("event_type") or "")
        if event_type not in SUMMARY_EVENT_TYPES:
            continue
        payload = row["payload"]
        strategy = _strategy(event_type, payload)
        family = _family(strategy)
        if family is None:
            continue
        if run_id and str(payload.get("phase183_run_id") or "") != run_id:
            continue
        if strategy_filter and strategy.upper() != strategy_filter:
            continue
        asset = _asset(payload)
        if asset_filter and asset != asset_filter:
            continue
        snapshot = payload.get("first_fill_snapshot")
        if not isinstance(snapshot, dict) or snapshot.get("complete_now_net_edge_per_share") is None:
            continue
        edge = _d(snapshot.get("complete_now_net_edge_per_share"))
        pnl = _d(
            payload.get("realized_pnl")
            if payload.get("realized_pnl") is not None
            else payload.get("pnl_usdc")
        )
        status = str(payload.get("status") or "UNKNOWN")
        true_win = status in TRUE_COMPLETION_STATUSES and pnl > 0
        item = groups[(family, _edge_bucket(edge))]
        item["n"] += 1
        item["pnl"] += pnl
        episode_id = payload.get("market_episode_id")
        if episode_id:
            item["episodes"].add(str(episode_id))
        if true_win:
            item["wins"] += 1
            if episode_id:
                item["win_episodes"].add(str(episode_id))
            else:
                item["legacy_win_episodes"] += 1

    lines = [
        "EDGE@FILL BY STRATEGY FAMILY / INDEPENDENT EPISODE",
        "N/WINS are model events; EP/WIN_EP deduplicate correlated variants using market_episode_id.",
    ]
    if not groups:
        lines.append("no family edge-at-fill samples")
        return "\n".join(lines)
    lines.extend(
        [
            f"{'FAMILY':10s} {'EDGE':18s} {'N':>5s} {'EP':>5s} {'WINS':>5s} {'WIN_EP':>6s} {'AVG PNL':>10s}",
            "-" * 70,
        ]
    )
    edge_order = [">=0", "-0.005..0", "-0.010..-0.005", "-0.015..-0.010", "-0.020..-0.015", "< -0.020"]
    for family in ("SHYB", "SPMAKER", "SMAKER"):
        for edge_label in edge_order:
            item = groups.get((family, edge_label))
            if not item:
                continue
            episodes = len(item["episodes"])
            win_episodes = len(item["win_episodes"]) + item["legacy_win_episodes"]
            avg_pnl = item["pnl"] / Decimal(item["n"])
            lines.append(
                f"{family:10s} {edge_label:18s} {item['n']:5d} {episodes:5d} {item['wins']:5d} "
                f"{win_episodes:6d} {float(avg_pnl):+10.5f}"
            )
    return "\n".join(lines)


def _ghost_tradeoff_table(
    path: Path,
    *,
    run_id: str | None,
    strategy_filter: str | None,
    asset_filter: str | None,
) -> str:
    # Focus on ANY_SIDE because it is the executable/conservative policy. The
    # ACTUAL_FIRST_SIDE rows in the core report are oracle diagnostics only.
    agg = defaultdict(
        lambda: {
            "n": 0,
            "avoid": 0,
            "loss_avoid": 0,
            "pos_kill": 0,
            "win_kill": 0,
            "cf_pnl": Decimal("0"),
            "delta": Decimal("0"),
        }
    )
    for row in _iter_rows(path) or ():
        if row.get("event_type") != "maker_variant_ghost_prefill_gate_result_v183":
            continue
        payload = row["payload"]
        if not _filter_event(
            payload,
            run_id=run_id,
            strategy=strategy_filter,
            asset=asset_filter,
        ):
            continue
        actual_pnl = _d(payload.get("actual_pnl"))
        status = str(payload.get("status") or "UNKNOWN")
        true_win = status in TRUE_COMPLETION_STATUSES and actual_pnl > 0
        for threshold, by_scope in (payload.get("results") or {}).items():
            scope = (by_scope or {}).get("ANY_SIDE") or {}
            for latency, outcome in (scope.get("latencies") or {}).items():
                item = agg[(str(threshold), int(latency))]
                item["n"] += 1
                avoided = bool(outcome.get("would_avoid_first_fill"))
                item["avoid"] += int(avoided)
                item["loss_avoid"] += int(avoided and actual_pnl < 0)
                item["pos_kill"] += int(avoided and actual_pnl > 0)
                item["win_kill"] += int(avoided and true_win)
                item["cf_pnl"] += _d(outcome.get("counterfactual_pnl"))
                item["delta"] += _d(outcome.get("delta_vs_actual"))

    lines = [
        "GHOST ANY-SIDE CANCEL TRADE-OFF",
        "A cancellation can improve P&L by avoiding losses while also killing profitable/true-win campaigns; zero-trade policies are not treated as success.",
    ]
    if not agg:
        lines.append("no Phase 1.8.3 ANY_SIDE ghost-gate outcomes yet")
        return "\n".join(lines)
    lines.extend(
        [
            f"{'EDGE<=':>8s} {'LAT':>5s} {'N':>5s} {'AVOID':>6s} {'RETAIN':>6s} {'LOSS_AV':>7s} {'POS_KILL':>8s} {'WIN_KILL':>8s} {'CF PNL':>10s} {'DELTA':>10s}",
            "-" * 105,
        ]
    )
    for (threshold, latency), item in sorted(
        agg.items(), key=lambda entry: (Decimal(entry[0][0]), entry[0][1])
    ):
        retain = item["n"] - item["avoid"]
        lines.append(
            f"{threshold:>8s} {latency:4d}ms {item['n']:5d} {item['avoid']:6d} {retain:6d} "
            f"{item['loss_avoid']:7d} {item['pos_kill']:8d} {item['win_kill']:8d} "
            f"{float(item['cf_pnl']):+10.4f} {float(item['delta']):+10.4f}"
        )
    return "\n".join(lines)


def cli() -> None:
    # Preserve the complete Phase 1.8.3 core report first.
    cli_v183()

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("path", nargs="?", default="data/shadow_events.jsonl")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--asset", default=None)
    parser.add_argument("--strategy", default=None)
    parser.add_argument("--session", nargs="?", const="latest", default=None)
    args, _ = parser.parse_known_args()

    path = Path(args.path)
    run_id = None
    if args.session:
        run_id = _latest_run_id(path) if args.session == "latest" else args.session
    strategy_filter = args.strategy.upper() if args.strategy else None
    asset_filter = args.asset.upper() if args.asset else None

    print()
    print(
        _family_edge_table(
            path,
            run_id=run_id,
            strategy_filter=strategy_filter,
            asset_filter=asset_filter,
        )
    )
    print()
    print(
        _ghost_tradeoff_table(
            path,
            run_id=run_id,
            strategy_filter=strategy_filter,
            asset_filter=asset_filter,
        )
    )


if __name__ == "__main__":
    cli()
