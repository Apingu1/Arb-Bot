from __future__ import annotations

import argparse
import json
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any


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


def _d(value: Any) -> Decimal:
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


def _metric_bucket() -> dict:
    return {
        "queue_imbalance": [],
        "first_fill_ms": [],
        "completion_edge": [],
        "small_queue": [],
        "max_queue": [],
    }


def _asset_bucket() -> dict:
    return {
        "events": 0,
        "pnl": Decimal("0"),
        "true_wins": 0,
        "loss_events": 0,
        "true_win_pnl": Decimal("0"),
        "loss_pnl": Decimal("0"),
        "episode_ids": set(),
        "legacy_win_episodes": 0,
        "first_fill_win": _metric_bucket(),
        "first_fill_loss": _metric_bucket(),
    }


def _bucket() -> dict:
    return {
        "events": 0,
        "pnl": Decimal("0"),
        "true_wins": 0,
        "loss_events": 0,
        "flat_events": 0,
        "true_win_pnl": Decimal("0"),
        "loss_pnl": Decimal("0"),
        "statuses": defaultdict(int),
        "episode_ids": set(),
        "legacy_win_episodes": 0,
        "assets": defaultdict(_asset_bucket),
        "first_fill_win": _metric_bucket(),
        "first_fill_loss": _metric_bucket(),
    }


def _append_metric(bucket: dict, key: str, value: Any) -> None:
    if value is None or value == "":
        return
    try:
        bucket[key].append(_d(value))
    except Exception:
        return


def _observe_first_fill(target: dict, payload: dict) -> None:
    snapshot = payload.get("first_fill_snapshot")
    if not isinstance(snapshot, dict):
        return
    _append_metric(target, "queue_imbalance", snapshot.get("queue_imbalance_initial"))
    _append_metric(target, "first_fill_ms", snapshot.get("first_fill_ms"))
    _append_metric(target, "completion_edge", snapshot.get("complete_now_net_edge_per_share"))
    _append_metric(target, "small_queue", snapshot.get("small_queue_initial"))
    _append_metric(target, "max_queue", snapshot.get("max_queue_initial"))


def _episodes(item: dict) -> int:
    return len(item.get("episode_ids") or ()) + int(item.get("legacy_win_episodes") or 0)


def build_compact(path: Path) -> dict:
    stats = defaultdict(_bucket)
    atomic = defaultdict(
        lambda: {
            "captures": 0,
            "pnl": Decimal("0"),
            "assets": defaultdict(int),
        }
    )
    malformed = 0
    lines = 0
    global_episode_ids: set[str] = set()
    global_legacy_wins = 0
    global_true_win_model_events = 0

    if not path.exists():
        return {
            "stats": stats,
            "atomic": atomic,
            "malformed": 0,
            "lines": 0,
            "global_episode_ids": global_episode_ids,
            "global_legacy_wins": global_legacy_wins,
            "global_true_win_model_events": global_true_win_model_events,
        }

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
            pnl = _d(
                payload.get("realized_pnl")
                if payload.get("realized_pnl") is not None
                else payload.get("pnl_usdc")
            )
            status = str(payload.get("status") or "UNKNOWN")
            asset = _asset(payload)
            true_win = status in TRUE_COMPLETION_STATUSES and pnl > 0
            loss = pnl < 0

            item = stats[strategy]
            item["events"] += 1
            item["pnl"] += pnl
            item["statuses"][status] += 1
            if true_win:
                item["true_wins"] += 1
                item["true_win_pnl"] += pnl
            elif loss:
                item["loss_events"] += 1
                item["loss_pnl"] += pnl
            elif pnl == 0:
                item["flat_events"] += 1

            asset_item = item["assets"][asset]
            asset_item["events"] += 1
            asset_item["pnl"] += pnl
            if true_win:
                asset_item["true_wins"] += 1
                asset_item["true_win_pnl"] += pnl
            elif loss:
                asset_item["loss_events"] += 1
                asset_item["loss_pnl"] += pnl

            episode_id = payload.get("market_episode_id")
            if true_win:
                global_true_win_model_events += 1
                if episode_id:
                    episode_text = str(episode_id)
                    item["episode_ids"].add(episode_text)
                    asset_item["episode_ids"].add(episode_text)
                    global_episode_ids.add(episode_text)
                else:
                    item["legacy_win_episodes"] += 1
                    asset_item["legacy_win_episodes"] += 1
                    global_legacy_wins += 1

            if true_win:
                _observe_first_fill(item["first_fill_win"], payload)
                _observe_first_fill(asset_item["first_fill_win"], payload)
            elif loss:
                _observe_first_fill(item["first_fill_loss"], payload)
                _observe_first_fill(asset_item["first_fill_loss"], payload)

    return {
        "stats": stats,
        "atomic": atomic,
        "malformed": malformed,
        "lines": lines,
        "global_episode_ids": global_episode_ids,
        "global_legacy_wins": global_legacy_wins,
        "global_true_win_model_events": global_true_win_model_events,
    }


def _active(strategy: str) -> bool:
    if strategy in {"HYBRID-99", "HYBRID-98", "PMAKER-Q100", "PMAKER-Q250"}:
        return True
    return strategy.startswith(("SHYB-", "SPMAKER-", "SMAKER-"))


def _state(strategy: str, true_wins: int) -> str:
    if strategy.startswith(("SHYB-", "SPMAKER-", "SMAKER-")):
        return "SELECTIVE"
    if _active(strategy):
        return "BASELINE"
    if true_wins > 0:
        return "HISTORICAL-WINNER"
    return "HISTORICAL"


def _avg(total: Decimal, count: int) -> Decimal:
    return total / Decimal(count) if count else Decimal("0")


def _med(values: list[Decimal]) -> Decimal | None:
    return median(values) if values else None


def _fmt_metric(value: Decimal | None, digits: int = 2) -> str:
    if value is None:
        return "-"
    return f"{float(value):.{digits}f}"


def cli() -> None:
    parser = argparse.ArgumentParser(
        description="Compact Phase 1.8.1 selective shadow-arbitrage report"
    )
    parser.add_argument("path", nargs="?", default="data/shadow_events.jsonl")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Show every historical strategy instead of winner/selective output",
    )
    parser.add_argument("--asset", default=None, help="Optional asset filter, e.g. ETH")
    parser.add_argument(
        "--strategy",
        default=None,
        help="Optional exact strategy filter, e.g. SHYB-97-I2",
    )
    args = parser.parse_args()

    path = Path(args.path)
    result = build_compact(path)
    stats = result["stats"]
    asset_filter = args.asset.upper() if args.asset else None
    strategy_filter = args.strategy.upper() if args.strategy else None

    print(f"ARB REPORT // {path}")
    print(f"lines={result['lines']} malformed={result['malformed']}")
    print(
        "TRUE WIN = profitable completed complete-set only; "
        "WIN_EP = independent clustered winning episodes when available"
    )
    independent = len(result["global_episode_ids"]) + result["global_legacy_wins"]
    print(
        f"maker-family true-win model events={result['global_true_win_model_events']} "
        f"independent winning episodes={independent}"
    )
    print()

    rows = []
    for strategy, item in stats.items():
        if strategy_filter and strategy.upper() != strategy_filter:
            continue
        if not args.all and not (_active(strategy) or item["true_wins"] > 0):
            continue
        if asset_filter:
            detail = item["assets"].get(asset_filter)
            if not detail or detail["events"] <= 0:
                continue
        else:
            detail = item
        rows.append((strategy, detail))

    rows.sort(
        key=lambda row: (
            row[1]["true_wins"],
            row[1]["pnl"],
        ),
        reverse=True,
    )

    print(
        f"{'STRATEGY':24s} {'WINS':>5s} {'WIN_EP':>6s} {'EVENTS':>7s} "
        f"{'WIN%':>7s} {'AVG WIN':>10s} {'AVG LOSS':>10s} {'P&L':>11s}  STATE"
    )
    print("-" * 108)
    for strategy, detail in rows:
        wins = detail["true_wins"]
        events = detail["events"]
        win_rate = Decimal(wins) / Decimal(events) if events else Decimal("0")
        avg_win = _avg(detail["true_win_pnl"], wins)
        avg_loss = _avg(detail["loss_pnl"], detail["loss_events"])
        print(
            f"{strategy:24s} {wins:5d} {_episodes(detail):6d} {events:7d} "
            f"{float(win_rate * 100):6.1f}% {float(avg_win):+10.5f} "
            f"{float(avg_loss):+10.5f} {float(detail['pnl']):+11.4f}  "
            f"{_state(strategy, wins)}"
        )

    print("\nMODEL × ASSET")
    print(
        f"{'STRATEGY':24s} {'ASSET':>6s} {'WINS':>5s} {'WIN_EP':>6s} "
        f"{'EVENTS':>7s} {'WIN%':>7s} {'AVG WIN':>10s} {'AVG LOSS':>10s} {'P&L':>11s}"
    )
    print("-" * 104)
    for strategy, item in sorted(stats.items()):
        if strategy_filter and strategy.upper() != strategy_filter:
            continue
        if not args.all and not (_active(strategy) or item["true_wins"] > 0):
            continue
        for asset, detail in sorted(item["assets"].items()):
            if asset_filter and asset != asset_filter:
                continue
            wins = detail["true_wins"]
            events = detail["events"]
            win_rate = Decimal(wins) / Decimal(events) if events else Decimal("0")
            print(
                f"{strategy:24s} {asset:>6s} {wins:5d} {_episodes(detail):6d} "
                f"{events:7d} {float(win_rate * 100):6.1f}% "
                f"{float(_avg(detail['true_win_pnl'], wins)):+10.5f} "
                f"{float(_avg(detail['loss_pnl'], detail['loss_events'])):+10.5f} "
                f"{float(detail['pnl']):+11.4f}"
            )

    selective_rows = []
    for strategy, item in stats.items():
        if not strategy.startswith(("SHYB-", "SPMAKER-", "SMAKER-")):
            continue
        if strategy_filter and strategy.upper() != strategy_filter:
            continue
        for asset, detail in item["assets"].items():
            if asset_filter and asset != asset_filter:
                continue
            win_metrics = detail["first_fill_win"]
            loss_metrics = detail["first_fill_loss"]
            samples = len(win_metrics["first_fill_ms"]) + len(loss_metrics["first_fill_ms"])
            if samples:
                selective_rows.append((strategy, asset, detail))

    print("\nSELECTIVE FIRST-FILL DIAGNOSTICS")
    if not selective_rows:
        print("no selective first-fill samples yet")
    else:
        print(
            f"{'STRATEGY':24s} {'ASSET':>6s} {'W/L':>7s} "
            f"{'QIMB W/L':>15s} {'FILLms W/L':>17s} {'EDGE@FILL W/L':>21s}"
        )
        print("-" * 98)
        for strategy, asset, detail in sorted(selective_rows):
            w = detail["first_fill_win"]
            l = detail["first_fill_loss"]
            qimb = f"{_fmt_metric(_med(w['queue_imbalance']))}/{_fmt_metric(_med(l['queue_imbalance']))}"
            fillms = f"{_fmt_metric(_med(w['first_fill_ms']),1)}/{_fmt_metric(_med(l['first_fill_ms']),1)}"
            edge = f"{_fmt_metric(_med(w['completion_edge']),4)}/{_fmt_metric(_med(l['completion_edge']),4)}"
            print(
                f"{strategy:24s} {asset:>6s} "
                f"{detail['true_wins']:3d}/{detail['loss_events']:<3d} "
                f"{qimb:>15s} {fillms:>17s} {edge:>21s}"
            )

    print("\nIDEAL ATOMIC BENCHMARK (excluded from shadow P&L)")
    if not result["atomic"]:
        print("no atomic captures")
    else:
        for strategy, item in sorted(result["atomic"].items()):
            assets = ",".join(
                f"{key}:{value}"
                for key, value in sorted(item["assets"].items())
                if not asset_filter or key == asset_filter
            )
            if asset_filter and not assets:
                continue
            print(
                f"{strategy:24s} captures={item['captures']:5d} "
                f"benchmark_pnl={float(item['pnl']):+.5f} assets={assets or '-'}"
            )

    if not args.all:
        hidden = sum(
            1
            for strategy, item in stats.items()
            if not (_active(strategy) or item["true_wins"] > 0)
        )
        print(
            f"\n{hidden} winless/historical strategy rows hidden. "
            "Use --all to display them."
        )


if __name__ == "__main__":
    cli()
