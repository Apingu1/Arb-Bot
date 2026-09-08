from __future__ import annotations

import argparse
import json
from collections import defaultdict
from decimal import Decimal
from pathlib import Path


TRUE_COMPLETION_STATUSES = {
    "BOTH_MAKER_FILLED",
    "MAKER_PLUS_TAKER_COMPLETED",
    "BOTH_FILLED",
    "COMPLETE_SET",
}

SUMMARY_EVENT_TYPES = {
    "taker_execution_summary",
    "maker_execution_summary",
    "hybrid_execution_summary",
    "maker_variant_execution_summary",
    "hedge_execution_summary",
    "split_sell_execution_summary",
    "dual_fok_execution_summary",
}


def _d(value) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    return Decimal(str(value))


def _strategy(event_type: str, payload: dict) -> str:
    if payload.get("strategy"):
        return str(payload["strategy"])
    if event_type == "taker_execution_summary":
        return "TAKER"
    if event_type == "maker_execution_summary":
        return "MAKER"
    if event_type == "hybrid_execution_summary":
        return "HYBRID"
    return "UNKNOWN"


def _asset(payload: dict) -> str:
    if payload.get("asset"):
        return str(payload["asset"]).upper()
    slug = str(payload.get("slug") or "")
    return slug.split("-", 1)[0].upper() if "-" in slug else "UNKNOWN"


def build_compact(path: Path) -> dict:
    stats = defaultdict(lambda: {
        "events": 0,
        "pnl": Decimal("0"),
        "true_wins": 0,
        "loss_events": 0,
        "flat_events": 0,
        "statuses": defaultdict(int),
        "assets": defaultdict(lambda: {"events": 0, "pnl": Decimal("0"), "true_wins": 0}),
    })
    atomic = defaultdict(lambda: {"captures": 0, "pnl": Decimal("0"), "assets": defaultdict(int)})
    malformed = 0
    lines = 0

    if not path.exists():
        return {"stats": stats, "atomic": atomic, "malformed": 0, "lines": 0}

    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            lines += 1
            try:
                row = json.loads(line)
            except Exception:
                malformed += 1
                continue
            if not isinstance(row, dict):
                continue
            event_type = str(row.get("event_type") or "")
            payload = row.get("payload")
            if not isinstance(payload, dict):
                continue

            if event_type == "atomic_benchmark_capture":
                strategy = str(payload.get("strategy") or "ATOMIC")
                atomic[strategy]["captures"] += 1
                atomic[strategy]["pnl"] += _d(payload.get("realized_pnl"))
                atomic[strategy]["assets"][_asset(payload)] += 1
                continue

            if event_type not in SUMMARY_EVENT_TYPES:
                continue

            strategy = _strategy(event_type, payload)
            pnl = _d(payload.get("realized_pnl") if payload.get("realized_pnl") is not None else payload.get("pnl_usdc"))
            status = str(payload.get("status") or "UNKNOWN")
            asset = _asset(payload)
            true_win = status in TRUE_COMPLETION_STATUSES and pnl > 0

            item = stats[strategy]
            item["events"] += 1
            item["pnl"] += pnl
            item["statuses"][status] += 1
            if true_win:
                item["true_wins"] += 1
            elif pnl < 0:
                item["loss_events"] += 1
            elif pnl == 0:
                item["flat_events"] += 1

            a = item["assets"][asset]
            a["events"] += 1
            a["pnl"] += pnl
            a["true_wins"] += int(true_win)

    return {"stats": stats, "atomic": atomic, "malformed": malformed, "lines": lines}


def _active(strategy: str) -> bool:
    return strategy in {"HYBRID-99", "HYBRID-98", "PMAKER-Q100", "PMAKER-Q250"}


def cli() -> None:
    parser = argparse.ArgumentParser(description="Compact Phase 1.8 shadow-arbitrage report")
    parser.add_argument("path", nargs="?", default="data/shadow_events.jsonl")
    parser.add_argument("--all", action="store_true", help="Show every historical strategy instead of winner-focused output")
    parser.add_argument("--asset", default=None, help="Optional asset filter, e.g. ETH")
    args = parser.parse_args()

    path = Path(args.path)
    result = build_compact(path)
    stats = result["stats"]
    asset_filter = args.asset.upper() if args.asset else None

    print(f"ARB REPORT // {path}")
    print(f"lines={result['lines']} malformed={result['malformed']}")
    print("TRUE WIN = profitable completed complete-set only; favorable one-sided unwinds are not wins")
    print()

    rows = []
    for strategy, item in stats.items():
        if not args.all and not (_active(strategy) or item["true_wins"] > 0):
            continue
        if asset_filter:
            asset_row = item["assets"].get(asset_filter)
            if not asset_row or asset_row["events"] <= 0:
                continue
            rows.append((strategy, asset_row["true_wins"], asset_row["events"], asset_row["pnl"], item))
        else:
            rows.append((strategy, item["true_wins"], item["events"], item["pnl"], item))

    rows.sort(key=lambda r: (r[1], r[3]), reverse=True)
    print(f"{'STRATEGY':24s} {'TRUE_WINS':>9s} {'EVENTS':>7s} {'P&L pUSD':>12s}  STATE")
    print("-" * 70)
    for strategy, wins, events, pnl, item in rows:
        state = "ACTIVE" if _active(strategy) else "HISTORICAL-WINNER"
        print(f"{strategy:24s} {wins:9d} {events:7d} {float(pnl):+12.4f}  {state}")

    print("\nACTIVE MODEL × ASSET")
    print(f"{'STRATEGY':24s} {'ASSET':>7s} {'WINS':>6s} {'EVENTS':>7s} {'P&L':>11s}")
    print("-" * 62)
    for strategy, item in sorted(stats.items()):
        if not _active(strategy):
            continue
        for asset, detail in sorted(item["assets"].items()):
            if asset_filter and asset != asset_filter:
                continue
            print(f"{strategy:24s} {asset:>7s} {detail['true_wins']:6d} {detail['events']:7d} {float(detail['pnl']):+11.4f}")

    print("\nIDEAL ATOMIC BENCHMARK (excluded from shadow P&L)")
    if not result["atomic"]:
        print("no atomic captures")
    else:
        for strategy, item in sorted(result["atomic"].items()):
            assets = ",".join(f"{k}:{v}" for k, v in sorted(item["assets"].items()))
            print(f"{strategy:24s} captures={item['captures']:5d} benchmark_pnl={float(item['pnl']):+.5f} assets={assets or '-'}")

    if not args.all:
        hidden = sum(1 for strategy in stats if not (_active(strategy) or stats[strategy]["true_wins"] > 0))
        print(f"\n{hidden} winless/historical strategy rows hidden. Use --all to display them.")


if __name__ == "__main__":
    cli()
