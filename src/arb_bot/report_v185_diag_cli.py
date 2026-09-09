from __future__ import annotations

import argparse
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from statistics import median

from .report_v183 import _iter_rows, _latest_run_id
from .report_v181 import _asset, _d
from .report_v185_cli import cli as cli_v185


def _med(values: list[Decimal]) -> Decimal:
    return Decimal(str(median(values))) if values else Decimal("0")


def _gate_table(path: Path, *, run_id: str | None, asset_filter: str | None) -> str:
    reasons = defaultdict(int)
    edges: list[Decimal] = []
    coverages: list[Decimal] = []
    ages: list[Decimal] = []
    samples = 0

    for row in _iter_rows(path) or ():
        if row.get("event_type") != "profit_fok_gate_sample_v185":
            continue
        payload = row["payload"]
        if run_id and str(payload.get("phase183_run_id") or "") != run_id:
            continue
        if asset_filter and _asset(payload) != asset_filter:
            continue
        samples += 1
        reasons[str(payload.get("gate_reason") or "UNKNOWN")] += 1
        if payload.get("sample_edge_per_share") is not None:
            edges.append(_d(payload.get("sample_edge_per_share")))
        if payload.get("sample_coverage") is not None:
            coverages.append(_d(payload.get("sample_coverage")))
        for key in ("book_age_a_ms", "book_age_b_ms"):
            if payload.get(key) is not None:
                ages.append(_d(payload.get(key)))

    lines = [
        "PHASE 1.8.5 PFOK GATE DIAGNOSTICS",
        "Sampled every configured interval; these are observations, not trades and not P&L.",
    ]
    if samples == 0:
        lines.append("no PFOK gate samples in this session")
        return "\n".join(lines)

    lines.append(
        f"samples={samples} p50_edge={float(_med(edges)):+.5f}/sh "
        f"p50_coverage={float(_med(coverages)):.2f}x p50_book_age={float(_med(ages)):.2f}ms"
    )
    lines.append("gate_reasons=" + ", ".join(
        f"{reason}:{count}" for reason, count in sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))
    ))
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
