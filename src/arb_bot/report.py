from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any


SUMMARY_EVENT_TYPES = {
    "taker_execution_summary": "TAKER",
    "maker_execution_summary": "MAKER",
    "hybrid_execution_summary": "HYBRID",
}

RESEARCH_SUMMARY_EVENT_TYPES = {
    "maker_variant_execution_summary",
    "hedge_execution_summary",
    "split_sell_execution_summary",
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
    if event_type in RESEARCH_SUMMARY_EVENT_TYPES:
        strategy = str(payload.get("strategy") or "UNKNOWN")
    else:
        strategy = SUMMARY_EVENT_TYPES[event_type]
    recovery = payload.get("recovery") if isinstance(payload.get("recovery"), dict) else {}
    initial = payload.get("initial_execution") if isinstance(payload.get("initial_execution"), dict) else {}

    detected_pair = payload.get("detected_pair_vwap")
    if detected_pair is None and payload.get("maker_bid_a") is not None and payload.get("maker_bid_b") is not None:
        detected_pair = _d(payload.get("maker_bid_a")) + _d(payload.get("maker_bid_b"))

    return {
        "strategy": strategy,
        "mode": payload.get("mode"),
        "target_pair": payload.get("target_pair"),
        "target_net_edge_per_share": payload.get("target_net_edge_per_share"),
        "target_edge_per_share": payload.get("target_edge_per_share"),
        "completion_latency_ms": payload.get("completion_latency_ms"),
        "grace_ms": payload.get("grace_ms"),
        "fixed_size": payload.get("fixed_size"),
        "regime": payload.get("regime"),
        "finalized_at": payload.get("finalized_at"),
        "slug": payload.get("slug"),
        "status": payload.get("status"),
        "action": payload.get("action"),
        "shares": payload.get("shares") or payload.get("target_shares"),
        "maker_side": payload.get("maker_side"),
        "hedge_side": payload.get("hedge_side"),
        "maker_price": payload.get("maker_price"),
        "maker_fill_qty": payload.get("maker_fill_qty"),
        "sell_price_a": payload.get("sell_price_a"),
        "sell_price_b": payload.get("sell_price_b"),
        "taker_fee_paid": payload.get("taker_fee_paid"),
        "taker_rebate_pnl_scenarios": payload.get("taker_rebate_pnl_scenarios"),
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
        "filled_qty_a": payload.get("filled_qty_a"),
        "filled_qty_b": payload.get("filled_qty_b"),
        "matched_qty": payload.get("matched_qty"),
        "initial_queue_ahead_a": payload.get("initial_queue_ahead_a"),
        "initial_queue_ahead_b": payload.get("initial_queue_ahead_b"),
        "initial_queue_ahead": payload.get("initial_queue_ahead"),
        "maker_inventory_probability": payload.get("maker_inventory_probability"),
        "maker_avg_inventory_loss_per_share": payload.get("maker_avg_inventory_loss_per_share"),
        "maker_empirical_reserve_per_share": payload.get("maker_empirical_reserve_per_share"),
        "realized_pnl": payload.get("realized_pnl"),
        "equity_after": payload.get("equity_after"),
    }


def _flat_legacy_taker(payload: dict[str, Any], equity_after: Decimal) -> dict[str, Any]:
    return {
        "strategy": "TAKER", "mode": None, "target_pair": None,
        "target_net_edge_per_share": None, "target_edge_per_share": None,
        "completion_latency_ms": None, "grace_ms": None, "fixed_size": None, "regime": None,
        "finalized_at": None, "slug": payload.get("slug"), "status": payload.get("status"),
        "action": payload.get("action"), "shares": payload.get("shares"), "maker_side": None,
        "hedge_side": None, "maker_price": None, "maker_fill_qty": None,
        "sell_price_a": None, "sell_price_b": None, "taker_fee_paid": None,
        "taker_rebate_pnl_scenarios": None, "detected_pair_price": None, "detected_best_ask_a": None,
        "detected_best_ask_b": None, "execution_leg_a_avg": None, "execution_leg_b_avg": None,
        "execution_latency_ms": None, "recovery_latency_ms": None, "recovery_filled_leg": None,
        "recovery_completion_avg": None, "recovery_unwind_avg": None, "filled_qty_a": None,
        "filled_qty_b": None, "matched_qty": None, "initial_queue_ahead_a": None,
        "initial_queue_ahead_b": None, "initial_queue_ahead": None, "maker_inventory_probability": None,
        "maker_avg_inventory_loss_per_share": None, "maker_empirical_reserve_per_share": None,
        "realized_pnl": payload.get("pnl_usdc"), "equity_after": equity_after,
    }


def _research_activity(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "placements": 0,
            "cancellations": 0,
            "cancel_reasons": Counter(),
            "ghost_outcomes": 0,
            "ghost_fills": 0,
            "ghost_profitable": 0,
            "ghost_target_profitable": 0,
        }
    )
    for row in events:
        event_type = row.get("event_type")
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        strategy = str(payload.get("strategy") or "")
        if not strategy:
            continue
        if event_type in {"hedge_campaign_placed", "split_sell_campaign_placed"}:
            stats[strategy]["placements"] += 1
        elif event_type in {"hedge_campaign_cancelled", "split_sell_campaign_cancelled"}:
            stats[strategy]["cancellations"] += 1
            stats[strategy]["cancel_reasons"][str(payload.get("reason") or "UNKNOWN")] += 1
        elif event_type == "hedge_ghost_outcome":
            stats[strategy]["ghost_outcomes"] += 1
            if payload.get("outcome") == "WOULD_FILL":
                stats[strategy]["ghost_fills"] += 1
                if bool(payload.get("would_be_profitable")):
                    stats[strategy]["ghost_profitable"] += 1
                if bool(payload.get("would_clear_original_target")):
                    stats[strategy]["ghost_target_profitable"] += 1
    return stats


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
        elif event_type in RESEARCH_SUMMARY_EVENT_TYPES and isinstance(payload, dict):
            executions.append(_flat_execution(str(event_type), payload))

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

    activity = _research_activity(events)
    strategy_names = {str(row.get("strategy") or "UNKNOWN") for row in executions}
    strategy_names.update(activity.keys())
    ordered_names = sorted(
        strategy_names,
        key=lambda name: (
            0 if name == "TAKER" else
            1 if name.startswith("MAKER") else
            2 if name.startswith("HYBRID") else
            3 if name.startswith("HEDGE") else
            4 if name.startswith("EV-") else
            5 if name.startswith("SPLITSELL") else 6,
            name,
        ),
    )

    summary: dict[str, dict[str, Any]] = {}
    for strategy in ordered_names:
        rows = [row for row in executions if row["strategy"] == strategy]
        statuses = Counter(str(row.get("status") or "UNKNOWN") for row in rows)
        pnl = sum((_d(row.get("realized_pnl")) for row in rows), Decimal("0"))
        wins = sum(1 for row in rows if _d(row.get("realized_pnl")) > 0)
        losses = sum(1 for row in rows if _d(row.get("realized_pnl")) < 0)
        flats = len(rows) - wins - losses
        mid_rows = [row for row in rows if row.get("regime") == "MID"]
        extreme_rows = [row for row in rows if row.get("regime") == "EXTREME"]
        act = activity.get(strategy, {})
        summary[strategy] = {
            "events": len(rows), "wins": wins, "losses": losses, "flats": flats, "pnl": pnl,
            "statuses": statuses,
            "mid_events": len(mid_rows),
            "mid_pnl": sum((_d(row.get("realized_pnl")) for row in mid_rows), Decimal("0")),
            "extreme_events": len(extreme_rows),
            "extreme_pnl": sum((_d(row.get("realized_pnl")) for row in extreme_rows), Decimal("0")),
            "placements": int(act.get("placements", 0)),
            "cancellations": int(act.get("cancellations", 0)),
            "cancel_reasons": act.get("cancel_reasons", Counter()),
            "ghost_outcomes": int(act.get("ghost_outcomes", 0)),
            "ghost_fills": int(act.get("ghost_fills", 0)),
            "ghost_profitable": int(act.get("ghost_profitable", 0)),
            "ghost_target_profitable": int(act.get("ghost_target_profitable", 0)),
        }
    return summary, executions


def write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys()) if rows else ["strategy", "realized_pnl"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def cli() -> None:
    parser = argparse.ArgumentParser(description="Summarize TAKER, MAKER/HYBRID, HEDGE, EV-frontier and SPLITSELL shadow research")
    parser.add_argument("path", nargs="?", default="data/shadow_events.jsonl", help="Shadow JSONL path")
    parser.add_argument("--csv", dest="csv_path", default=None, help="Optional flat execution CSV output path")
    args = parser.parse_args()

    path = Path(args.path)
    summary, executions = build_report(path)
    print(f"Shadow strategy report: {path}")
    print("=" * 132)
    if not summary:
        print("No finalized strategy or research events found.")
    for strategy, item in summary.items():
        statuses = ", ".join(f"{key}={value}" for key, value in sorted(item["statuses"].items())) or "none"
        regime = ""
        if strategy.startswith(("HEDGE", "EV-")):
            regime = (
                f" | MID={item['mid_events']}/{float(item['mid_pnl']):+.4f} "
                f"EXTREME={item['extreme_events']}/{float(item['extreme_pnl']):+.4f}"
            )
        research = ""
        if item["placements"] or item["cancellations"] or item["ghost_outcomes"]:
            reason_text = ",".join(f"{k}:{v}" for k, v in sorted(item["cancel_reasons"].items())) or "-"
            research = (
                f" | placed={item['placements']} cancel={item['cancellations']}[{reason_text}]"
                f" ghost_fill={item['ghost_fills']}/{item['ghost_outcomes']}"
                f" ghost_profit={item['ghost_profitable']} target={item['ghost_target_profitable']}"
            )
        print(
            f"{strategy:24s} | events={item['events']:4d} wins={item['wins']:4d} "
            f"losses={item['losses']:4d} flats={item['flats']:4d} "
            f"pnl={float(item['pnl']):+.4f} pUSD | {statuses}{regime}{research}"
        )

    if args.csv_path:
        csv_path = Path(args.csv_path)
        write_csv(executions, csv_path)
        print(f"\nWrote {len(executions)} execution rows to {csv_path}")


if __name__ == "__main__":
    cli()
