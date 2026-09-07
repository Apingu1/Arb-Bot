from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any


SUMMARY_EVENT_TYPES = {
    "taker_execution_summary": "TAKER",
    "maker_execution_summary": "MAKER",
    "hybrid_execution_summary": "HYBRID",
}


def _d(value: Any) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    return Decimal(str(value))


def _load_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _flat_execution(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    strategy = SUMMARY_EVENT_TYPES[event_type]
    recovery = payload.get("recovery") if isinstance(payload.get("recovery"), dict) else {}
    initial = payload.get("initial_execution") if isinstance(payload.get("initial_execution"), dict) else {}

    detected_pair = payload.get("detected_pair_vwap")
    if detected_pair is None and payload.get("maker_bid_a") is not None and payload.get("maker_bid_b") is not None:
        detected_pair = _d(payload.get("maker_bid_a")) + _d(payload.get("maker_bid_b"))

    return {
        "strategy": strategy,
        "finalized_at": payload.get("finalized_at"),
        "slug": payload.get("slug"),
        "status": payload.get("status"),
        "action": payload.get("action"),
        "shares": payload.get("shares"),
        "detected_pair_price": detected_pair,
        "detected_best_ask_a": payload.get("detected_best_ask_a"),
        "detected_best_ask_b": payload.get("detected_best_ask_b"),
        "execution_leg_a_avg": (initial.get("leg_a") or {}).get("average_price") if isinstance(initial.get("leg_a"), dict) else None,
        "execution_leg_b_avg": (initial.get("leg_b") or {}).get("average_price") if isinstance(initial.get("leg_b"), dict) else None,
        "execution_latency_ms": payload.get("actual_execution_latency_ms") or initial.get("actual_latency_ms"),
        "recovery_latency_ms": recovery.get("actual_recovery_latency_ms"),
        "recovery_filled_leg": recovery.get("filled_leg"),
        "recovery_completion_avg": (recovery.get("completion_quote") or {}).get("average_price") if isinstance(recovery.get("completion_quote"), dict) else None,
        "recovery_unwind_avg": (recovery.get("unwind_quote") or {}).get("average_price") if isinstance(recovery.get("unwind_quote"), dict) else None,
        "realized_pnl": payload.get("realized_pnl"),
        "equity_after": payload.get("equity_after"),
    }


def _flat_legacy_taker(payload: dict[str, Any], equity_after: Decimal) -> dict[str, Any]:
    """Convert a Phase 1 legacy `shadow_result` into the report schema.

    Phase 1 did not persist the richer detection/execution metadata now available
    in Phase 1.2, so unavailable fields remain blank rather than being guessed.
    """
    return {
        "strategy": "TAKER",
        "finalized_at": None,
        "slug": payload.get("slug"),
        "status": payload.get("status"),
        "action": payload.get("action"),
        "shares": payload.get("shares"),
        "detected_pair_price": None,
        "detected_best_ask_a": None,
        "detected_best_ask_b": None,
        "execution_leg_a_avg": None,
        "execution_leg_b_avg": None,
        "execution_latency_ms": None,
        "recovery_latency_ms": None,
        "recovery_filled_leg": None,
        "recovery_completion_avg": None,
        "recovery_unwind_avg": None,
        "realized_pnl": payload.get("pnl_usdc"),
        "equity_after": equity_after,
    }


def build_report(path: Path) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    events = _load_rows(path)
    executions: list[dict[str, Any]] = []
    has_modern_taker_summary = False

    for row in events:
        event_type = row.get("event_type")
        payload = row.get("payload")
        if event_type in SUMMARY_EVENT_TYPES and isinstance(payload, dict):
            executions.append(_flat_execution(str(event_type), payload))
            if event_type == "taker_execution_summary":
                has_modern_taker_summary = True

    # Phase 1 wrote `shadow_result` but not `taker_execution_summary`. New
    # Phase 1.2 writes both, so only use legacy rows when there are no modern
    # taker summaries in the file; this prevents double-counting new runs.
    if not has_modern_taker_summary:
        legacy_equity = Decimal("0")
        for row in events:
            if row.get("event_type") != "shadow_result":
                continue
            payload = row.get("payload")
            if not isinstance(payload, dict):
                continue
            legacy_equity += _d(payload.get("pnl_usdc"))
            executions.append(_flat_legacy_taker(payload, legacy_equity))

    summary: dict[str, dict[str, Any]] = {}
    for strategy in ("TAKER", "MAKER", "HYBRID"):
        rows = [row for row in executions if row["strategy"] == strategy]
        statuses = Counter(str(row.get("status") or "UNKNOWN") for row in rows)
        pnl = sum((_d(row.get("realized_pnl")) for row in rows), Decimal("0"))
        wins = sum(1 for row in rows if _d(row.get("realized_pnl")) > 0)
        losses = sum(1 for row in rows if _d(row.get("realized_pnl")) < 0)
        flats = len(rows) - wins - losses
        summary[strategy] = {
            "events": len(rows),
            "wins": wins,
            "losses": losses,
            "flats": flats,
            "pnl": pnl,
            "statuses": statuses,
        }
    return summary, executions


def write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "strategy",
        "finalized_at",
        "slug",
        "status",
        "action",
        "shares",
        "detected_pair_price",
        "detected_best_ask_a",
        "detected_best_ask_b",
        "execution_leg_a_avg",
        "execution_leg_b_avg",
        "execution_latency_ms",
        "recovery_latency_ms",
        "recovery_filled_leg",
        "recovery_completion_avg",
        "recovery_unwind_avg",
        "realized_pnl",
        "equity_after",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def cli() -> None:
    parser = argparse.ArgumentParser(description="Summarize TAKER / MAKER / HYBRID shadow performance")
    parser.add_argument("path", nargs="?", default="data/shadow_events.jsonl", help="Shadow JSONL path")
    parser.add_argument("--csv", dest="csv_path", default=None, help="Optional flat execution CSV output path")
    args = parser.parse_args()

    path = Path(args.path)
    summary, executions = build_report(path)
    print(f"Shadow strategy report: {path}")
    print("=" * 78)
    for strategy in ("TAKER", "MAKER", "HYBRID"):
        item = summary[strategy]
        statuses = ", ".join(f"{key}={value}" for key, value in sorted(item["statuses"].items())) or "none"
        print(
            f"{strategy:6s} | events={item['events']:4d} wins={item['wins']:4d} "
            f"losses={item['losses']:4d} flats={item['flats']:4d} "
            f"pnl={float(item['pnl']):+.4f} pUSD | {statuses}"
        )

    if args.csv_path:
        csv_path = Path(args.csv_path)
        write_csv(executions, csv_path)
        print(f"\nWrote {len(executions)} execution rows to {csv_path}")


if __name__ == "__main__":
    cli()
