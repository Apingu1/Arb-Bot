from __future__ import annotations

import argparse
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any

from .report_v183 import _iter_rows, _latest_run_id
from .report_v181 import _asset, _d
from .report_v185_cli import cli as cli_v185


def _med(values: list[Decimal]) -> Decimal:
    return Decimal(str(median(values))) if values else Decimal("0")


def _gate_table(path: Path, *, run_id: str | None, asset_filter: str | None) -> str:
    stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "samples": 0,
            "reasons": defaultdict(int),
            "edges": [],
            "coverages": [],
            "ages": [],
        }
    )

    for row in _iter_rows(path) or ():
        if row.get("event_type") != "profit_fok_gate_sample_v185":
            continue
        payload = row["payload"]
        strategy = str(payload.get("strategy") or "")
        if not strategy.startswith("PFOK"):
            continue
        if run_id and str(payload.get("phase183_run_id") or "") != run_id:
            continue
        if asset_filter and _asset(payload) != asset_filter:
            continue

        item = stats[strategy]
        item["samples"] += 1
        item["reasons"][str(payload.get("gate_reason") or "UNKNOWN")] += 1
        if payload.get("sample_edge_per_share") is not None:
            item["edges"].append(_d(payload.get("sample_edge_per_share")))
        if payload.get("sample_coverage") is not None:
            item["coverages"].append(_d(payload.get("sample_coverage")))
        for key in ("book_age_a_ms", "book_age_b_ms"):
            if payload.get(key) is not None:
                item["ages"].append(_d(payload.get(key)))

    lines = [
        "PHASE 1.8.5 PFOK GATE DIAGNOSTICS",
        "Control samples more frequently than experimental variants; compare percentages/reasons, not raw sample totals.",
    ]
    if not stats:
        lines.append("no PFOK gate samples in this session")
        return "\n".join(lines)

    lines.extend(
        [
            f"{'STRATEGY':16s} {'SAMPLES':>8s} {'QUAL%':>7s} {'P50 EDGE':>10s} {'P50 COV':>8s} {'P50 AGE':>9s} {'TOP BLOCK':>23s}",
            "-" * 89,
        ]
    )
    names = ["PFOK", "PFOK-EDGE3", "PFOK-DEPTH1", "PFOK-NOSURGE", "PFOK-AGGR", "PFOK-S10", "PFOK-S20"]
    names.extend(sorted(set(stats) - set(names)))
    for strategy in names:
        item = stats.get(strategy)
        if item is None:
            continue
        samples = int(item["samples"])
        qualified = int(item["reasons"].get("QUALIFIED", 0))
        blocks = [(k, v) for k, v in item["reasons"].items() if k != "QUALIFIED"]
        top_block = max(blocks, key=lambda kv: kv[1])[0] if blocks else "NONE"
        lines.append(
            f"{strategy:16s} {samples:8d} {(qualified/samples*100 if samples else 0):6.2f}% "
            f"{float(_med(item['edges'])):+10.5f} {float(_med(item['coverages'])):7.2f}x "
            f"{float(_med(item['ages'])):8.2f}ms {top_block:>23s}"
        )

    control = stats.get("PFOK")
    if control is not None:
        lines.append("")
        lines.append(
            "PFOK_control_gate_reasons="
            + ", ".join(
                f"{reason}:{count}"
                for reason, count in sorted(control["reasons"].items(), key=lambda kv: (-kv[1], kv[0]))
            )
        )
    return "\n".join(lines)


def cli() -> None:
    cli_v185()

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
    print(_gate_table(path, run_id=run_id, asset_filter=asset_filter))


if __name__ == "__main__":
    cli()
