from __future__ import annotations

import argparse
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from .report_v181 import TRUE_COMPLETION_STATUSES, _asset, _d, _med
from .report_v183 import _filter_event, _iter_rows, _latest_run_id
from .report_v183_cli import cli as cli_v183


def _placement_reject_table(
    path: Path,
    *,
    run_id: str | None,
    strategy_filter: str | None,
    asset_filter: str | None,
) -> str:
    grouped = defaultdict(
        lambda: {
            "n": 0,
            "edges": [],
            "book_ages": [],
            "reasons": defaultdict(int),
        }
    )
    for row in _iter_rows(path) or ():
        if row.get("event_type") != "maker_variant_placement_reject_v184":
            continue
        payload = row["payload"]
        if not _filter_event(
            payload,
            run_id=run_id,
            strategy=strategy_filter,
            asset=asset_filter,
        ):
            continue
        name = str(payload.get("strategy") or "UNKNOWN")
        item = grouped[name]
        item["n"] += 1
        item["reasons"][str(payload.get("reason") or "UNKNOWN")] += 1
        if payload.get("worst_edge_per_share") is not None:
            item["edges"].append(_d(payload.get("worst_edge_per_share")))
        ages = [
            _d(payload.get("book_age_a_ms"))
            if payload.get("book_age_a_ms") is not None
            else None,
            _d(payload.get("book_age_b_ms"))
            if payload.get("book_age_b_ms") is not None
            else None,
        ]
        valid_ages = [value for value in ages if value is not None]
        if valid_ages:
            item["book_ages"].append(max(valid_ages))

    lines = [
        "PHASE 1.8.4 SAFE-ENTRY GATE",
        "Candidates below are rejected before a maker campaign is placed; they are not zero-P&L trades and must not be counted as wins.",
    ]
    if not grouped:
        lines.append("no Phase 1.8.4 placement-gate rejects yet")
        return "\n".join(lines)
    lines.extend(
        [
            f"{'STRATEGY':24s} {'REJECTS':>7s} {'P50 WORST EDGE':>15s} {'P50 BOOK AGE':>13s} {'TOP REASON':>26s}",
            "-" * 92,
        ]
    )
    for name, item in sorted(grouped.items()):
        top_reason = max(item["reasons"], key=item["reasons"].get) if item["reasons"] else "-"
        lines.append(
            f"{name:24s} {item['n']:7d} {float(_med(item['edges']) or 0):+15.5f} "
            f"{float(_med(item['book_ages']) or 0):11.1f}ms {top_reason:>26s}"
        )
    return "\n".join(lines)


def _fast_cancel_table(
    path: Path,
    *,
    run_id: str | None,
    strategy_filter: str | None,
    asset_filter: str | None,
) -> str:
    grouped = defaultdict(
        lambda: {
            "intent": 0,
            "effective": 0,
            "race": 0,
            "edges": [],
            "ages": [],
            "reasons": defaultdict(int),
        }
    )
    event_map = {
        "maker_variant_fast_cancel_intent_v184": "intent",
        "maker_variant_fast_cancel_effective_v184": "effective",
        "maker_variant_fast_cancel_race_lost_v184": "race",
    }
    for row in _iter_rows(path) or ():
        bucket = event_map.get(str(row.get("event_type") or ""))
        if bucket is None:
            continue
        payload = row["payload"]
        if not _filter_event(
            payload,
            run_id=run_id,
            strategy=strategy_filter,
            asset=asset_filter,
        ):
            continue
        name = str(payload.get("strategy") or "UNKNOWN")
        item = grouped[name]
        item[bucket] += 1
        reason = str(payload.get("reason") or "UNKNOWN")
        if bucket == "intent":
            item["reasons"][reason] += 1
            if payload.get("trigger_edge_per_share") is not None:
                item["edges"].append(_d(payload.get("trigger_edge_per_share")))
            if payload.get("trigger_quote_age_ms") is not None:
                item["ages"].append(_d(payload.get("trigger_quote_age_ms")))

    lines = [
        "PHASE 1.8.4 FAST PRE-FILL CANCELLATION",
        "EFFECTIVE means the configured cancellation latency elapsed before the first simulated maker fill; RACE means the fill beat the cancel.",
    ]
    if not grouped:
        lines.append("no Phase 1.8.4 fast-cancel events yet")
        return "\n".join(lines)
    lines.extend(
        [
            f"{'STRATEGY':24s} {'INTENT':>7s} {'EFFECT':>7s} {'RACE':>6s} {'WIN%':>7s} {'P50 EDGE':>10s} {'P50 AGE':>9s} {'TOP REASON':>22s}",
            "-" * 102,
        ]
    )
    for name, item in sorted(grouped.items()):
        decided = item["effective"] + item["race"]
        win_pct = item["effective"] / decided * 100 if decided else 0.0
        top_reason = max(item["reasons"], key=item["reasons"].get) if item["reasons"] else "-"
        lines.append(
            f"{name:24s} {item['intent']:7d} {item['effective']:7d} {item['race']:6d} "
            f"{win_pct:6.1f}% {float(_med(item['edges']) or 0):+10.5f} "
            f"{float(_med(item['ages']) or 0):8.1f}ms {top_reason:>22s}"
        )
    return "\n".join(lines)


def _prefill_timeline_table(
    path: Path,
    *,
    run_id: str | None,
    strategy_filter: str | None,
    asset_filter: str | None,
) -> str:
    grouped = defaultdict(
        lambda: {
            "edges": [],
            "win_edges": [],
            "loss_edges": [],
            "pnls": [],
            "wins": 0,
            "n": 0,
        }
    )
    for row in _iter_rows(path) or ():
        if row.get("event_type") != "maker_variant_prefill_timeline_outcome_v184":
            continue
        payload = row["payload"]
        if not _filter_event(
            payload,
            run_id=run_id,
            strategy=strategy_filter,
            asset=asset_filter,
        ):
            continue
        pnl = _d(payload.get("actual_pnl"))
        status = str(payload.get("actual_status") or "UNKNOWN")
        true_win = status in TRUE_COMPLETION_STATUSES and pnl > 0
        for lookback, sample in (payload.get("lookback_samples") or {}).items():
            if not isinstance(sample, dict) or sample.get("edge_per_share") is None:
                continue
            edge = _d(sample.get("edge_per_share"))
            item = grouped[int(lookback)]
            item["n"] += 1
            item["edges"].append(edge)
            item["pnls"].append(pnl)
            if true_win:
                item["wins"] += 1
                item["win_edges"].append(edge)
            else:
                item["loss_edges"].append(edge)

    lines = [
        "PHASE 1.8.4 PRE-FILL EDGE LOOKBACK",
        "Uses the actual future first-fill side only for diagnosis; it is not itself an executable side-selection rule.",
    ]
    if not grouped:
        lines.append("no finalized Phase 1.8.4 pre-fill timelines yet")
        return "\n".join(lines)
    lines.extend(
        [
            f"{'BEFORE FILL':>11s} {'N':>5s} {'WIN%':>7s} {'EDGE P50':>10s} {'WIN EDGE':>10s} {'NONWIN EDGE':>12s} {'AVG PNL':>10s}",
            "-" * 83,
        ]
    )
    for lookback in sorted(grouped, reverse=True):
        item = grouped[lookback]
        avg_pnl = sum(item["pnls"], Decimal("0")) / Decimal(item["n"])
        lines.append(
            f"{lookback:9d}ms {item['n']:5d} {item['wins']/item['n']*100:6.1f}% "
            f"{float(_med(item['edges']) or 0):+10.5f} "
            f"{float(_med(item['win_edges']) or 0):+10.5f} "
            f"{float(_med(item['loss_edges']) or 0):+12.5f} {float(avg_pnl):+10.5f}"
        )
    return "\n".join(lines)


def _atomic_execution_table(
    path: Path,
    *,
    run_id: str | None,
    strategy_filter: str | None,
    asset_filter: str | None,
) -> str:
    grouped = defaultdict(
        lambda: {
            "n": 0,
            "fills": 0,
            "pnl": Decimal("0"),
            "edges": [],
            "outcomes": defaultdict(int),
        }
    )
    for row in _iter_rows(path) or ():
        if row.get("event_type") != "atomic_execution_proxy_v184":
            continue
        payload = row["payload"]
        if run_id and str(payload.get("phase183_run_id") or "") != run_id:
            continue
        if strategy_filter and str(payload.get("strategy") or "").upper() != strategy_filter:
            continue
        if asset_filter and _asset(payload) != asset_filter:
            continue
        key = (
            str(payload.get("strategy") or "UNKNOWN"),
            int(payload.get("target_latency_ms") or 0),
        )
        item = grouped[key]
        item["n"] += 1
        outcome = str(payload.get("outcome") or "UNKNOWN")
        item["outcomes"][outcome] += 1
        if outcome == "EXECUTABLE_SHADOW_FILL":
            item["fills"] += 1
            item["pnl"] += _d(payload.get("pnl"))
            if payload.get("edge_per_share") is not None:
                item["edges"].append(_d(payload.get("edge_per_share")))

    lines = [
        "PHASE 1.8.4 ATOMIC EXECUTION PROXY",
        "Full requested size is re-quoted after end-to-end latency with fees and a 25 ms freshness gate. BUY/SELL variants remain mirrored benchmark views; do not add them as independent opportunities.",
    ]
    if not grouped:
        lines.append("no Phase 1.8.4 atomic execution-proxy attempts yet")
        return "\n".join(lines)
    lines.extend(
        [
            f"{'STRATEGY':24s} {'LAT':>5s} {'N':>5s} {'FILLS':>6s} {'FILL%':>7s} {'P50 EDGE':>10s} {'PROXY PNL':>11s}",
            "-" * 78,
        ]
    )
    for (name, latency), item in sorted(grouped.items()):
        lines.append(
            f"{name:24s} {latency:4d}ms {item['n']:5d} {item['fills']:6d} "
            f"{item['fills']/item['n']*100:6.1f}% {float(_med(item['edges']) or 0):+10.5f} "
            f"{float(item['pnl']):+11.5f}"
        )
    return "\n".join(lines)


def cli() -> None:
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
        _placement_reject_table(
            path,
            run_id=run_id,
            strategy_filter=strategy_filter,
            asset_filter=asset_filter,
        )
    )
    print()
    print(
        _fast_cancel_table(
            path,
            run_id=run_id,
            strategy_filter=strategy_filter,
            asset_filter=asset_filter,
        )
    )
    print()
    print(
        _prefill_timeline_table(
            path,
            run_id=run_id,
            strategy_filter=strategy_filter,
            asset_filter=asset_filter,
        )
    )
    print()
    print(
        _atomic_execution_table(
            path,
            run_id=run_id,
            strategy_filter=strategy_filter,
            asset_filter=asset_filter,
        )
    )


if __name__ == "__main__":
    cli()
