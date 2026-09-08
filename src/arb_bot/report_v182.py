from __future__ import annotations

import argparse
import json
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any

from .report_v181 import (
    SUMMARY_EVENT_TYPES,
    TRUE_COMPLETION_STATUSES,
    _active,
    _asset,
    _asset_bucket,
    _avg,
    _bucket,
    _d,
    _episodes,
    _fmt_metric,
    _med,
    _observe_first_fill,
    _state,
    _strategy,
)


EDGE_BUCKET_ORDER = (
    ">= 0",
    "-0.005..0",
    "-0.010..-0.005",
    "-0.015..-0.010",
    "-0.020..-0.015",
    "< -0.020",
)
ATOMIC_SURVIVAL_MS = (1, 2, 5, 10, 25, 50)


def _edge_bucket(value: Decimal) -> str:
    if value >= Decimal("0"):
        return ">= 0"
    if value >= Decimal("-0.005"):
        return "-0.005..0"
    if value >= Decimal("-0.010"):
        return "-0.010..-0.005"
    if value >= Decimal("-0.015"):
        return "-0.015..-0.010"
    if value >= Decimal("-0.020"):
        return "-0.020..-0.015"
    return "< -0.020"


def _percentile(values: list[Decimal], pct: float) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = pct * (len(ordered) - 1)
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    weight = Decimal(str(index - lower))
    return ordered[lower] * (Decimal("1") - weight) + ordered[upper] * weight


def _atomic_bucket() -> dict[str, Any]:
    return {
        "captures": 0,
        "pnl": Decimal("0"),
        "assets": defaultdict(int),
        "asset_pnl": defaultdict(lambda: Decimal("0")),
        "lifetimes": [],
        "asset_lifetimes": defaultdict(list),
        "instrumented_captures": 0,
        "instrumented_asset_captures": defaultdict(int),
        "latency_samples": defaultdict(list),
    }


def build_compact_v182(path: Path) -> dict[str, Any]:
    stats = defaultdict(_bucket)
    atomic = defaultdict(_atomic_bucket)
    selective_records: list[dict[str, Any]] = []
    malformed = 0
    lines = 0
    global_episode_ids: set[str] = set()
    global_legacy_wins = 0
    global_true_win_model_events = 0

    if not path.exists():
        return {
            "stats": stats,
            "atomic": atomic,
            "selective_records": selective_records,
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
                asset = _asset(payload)
                item = atomic[strategy]
                item["captures"] += 1
                item["pnl"] += _d(payload.get("realized_pnl"))
                item["assets"][asset] += 1
                item["asset_pnl"][asset] += _d(payload.get("realized_pnl"))
                if payload.get("phase182_latency_instrumented"):
                    item["instrumented_captures"] += 1
                    item["instrumented_asset_captures"][asset] += 1
                continue

            if event_type == "atomic_benchmark_lifetime":
                strategy = str(payload.get("strategy") or "ATOMIC")
                asset = _asset(payload)
                lifetime = payload.get("lifetime_ms")
                if lifetime is not None:
                    value = _d(lifetime)
                    atomic[strategy]["lifetimes"].append(value)
                    atomic[strategy]["asset_lifetimes"][asset].append(value)
                continue

            if event_type == "atomic_benchmark_latency_sample":
                strategy = str(payload.get("strategy") or "ATOMIC")
                atomic[strategy]["latency_samples"][int(payload.get("target_latency_ms") or 0)].append(
                    {
                        "asset": _asset(payload),
                        "actual_elapsed_ms": _d(payload.get("actual_elapsed_ms")),
                        "edge": _d(payload.get("edge_per_share")),
                        "pnl": _d(payload.get("pnl")),
                    }
                )
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

            if strategy.startswith(("SHYB-", "SPMAKER-", "SMAKER-")):
                snapshot = payload.get("first_fill_snapshot")
                selective_records.append(
                    {
                        "strategy": strategy,
                        "asset": asset,
                        "status": status,
                        "pnl": pnl,
                        "true_win": true_win,
                        "positive_noncomplete": pnl > 0 and not true_win,
                        "loss": loss,
                        "flat": pnl == 0,
                        "edge_at_fill": (
                            _d(snapshot.get("complete_now_net_edge_per_share"))
                            if isinstance(snapshot, dict)
                            and snapshot.get("complete_now_net_edge_per_share") is not None
                            else None
                        ),
                        "first_fill_ms": (
                            _d(snapshot.get("first_fill_ms"))
                            if isinstance(snapshot, dict)
                            and snapshot.get("first_fill_ms") is not None
                            else None
                        ),
                        "fill_to_finalize_ms": (
                            _d(payload.get("first_fill_to_finalize_ms"))
                            if payload.get("first_fill_to_finalize_ms") is not None
                            else None
                        ),
                        "timeline": payload.get("post_fill_edge_timeline")
                        if isinstance(payload.get("post_fill_edge_timeline"), dict)
                        else {},
                    }
                )

    return {
        "stats": stats,
        "atomic": atomic,
        "selective_records": selective_records,
        "malformed": malformed,
        "lines": lines,
        "global_episode_ids": global_episode_ids,
        "global_legacy_wins": global_legacy_wins,
        "global_true_win_model_events": global_true_win_model_events,
    }


def _filtered_records(
    records: list[dict[str, Any]],
    *,
    strategy_filter: str | None,
    asset_filter: str | None,
) -> list[dict[str, Any]]:
    return [
        record
        for record in records
        if (not strategy_filter or record["strategy"].upper() == strategy_filter)
        and (not asset_filter or record["asset"] == asset_filter)
    ]


def cli() -> None:
    parser = argparse.ArgumentParser(
        description="Compact Phase 1.8.2 edge/timing shadow-arbitrage report"
    )
    parser.add_argument("path", nargs="?", default="data/shadow_events.jsonl")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--asset", default=None, help="Optional asset filter, e.g. DOGE")
    parser.add_argument("--strategy", default=None, help="Optional exact strategy filter")
    args = parser.parse_args()

    path = Path(args.path)
    result = build_compact_v182(path)
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
        detail = item["assets"].get(asset_filter) if asset_filter else item
        if not detail or detail["events"] <= 0:
            continue
        rows.append((strategy, detail))

    rows.sort(key=lambda row: (row[1]["true_wins"], row[1]["pnl"]), reverse=True)
    print(
        f"{'STRATEGY':24s} {'WINS':>5s} {'WIN_EP':>6s} {'EVENTS':>7s} "
        f"{'WIN%':>7s} {'AVG WIN':>10s} {'AVG LOSS':>10s} {'P&L':>11s}  STATE"
    )
    print("-" * 108)
    for strategy, detail in rows:
        wins = detail["true_wins"]
        events = detail["events"]
        win_rate = Decimal(wins) / Decimal(events) if events else Decimal("0")
        print(
            f"{strategy:24s} {wins:5d} {_episodes(detail):6d} {events:7d} "
            f"{float(win_rate * 100):6.1f}% "
            f"{float(_avg(detail['true_win_pnl'], wins)):+10.5f} "
            f"{float(_avg(detail['loss_pnl'], detail['loss_events'])):+10.5f} "
            f"{float(detail['pnl']):+11.4f}  {_state(strategy, wins)}"
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
            w = detail["first_fill_win"]
            l = detail["first_fill_loss"]
            if len(w["first_fill_ms"]) + len(l["first_fill_ms"]):
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
                f"{strategy:24s} {asset:>6s} {detail['true_wins']:3d}/{detail['loss_events']:<3d} "
                f"{qimb:>15s} {fillms:>17s} {edge:>21s}"
            )

    selected = _filtered_records(
        result["selective_records"],
        strategy_filter=strategy_filter,
        asset_filter=asset_filter,
    )
    edge_records = [record for record in selected if record["edge_at_fill"] is not None]
    print("\nSELECTIVE EDGE@FILL BUCKETS")
    if not edge_records:
        print("no edge-at-fill samples yet")
    else:
        buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for record in edge_records:
            buckets[_edge_bucket(record["edge_at_fill"])].append(record)
        print(
            f"{'EDGE BUCKET':18s} {'N':>5s} {'WINS':>5s} {'WIN%':>7s} "
            f"{'AVG PNL':>10s} {'P50 FILLms':>11s} {'P50 F->END':>11s}"
        )
        print("-" * 75)
        for label in EDGE_BUCKET_ORDER:
            records = buckets.get(label, [])
            if not records:
                continue
            wins = sum(1 for record in records if record["true_win"])
            avg_pnl = sum((record["pnl"] for record in records), Decimal("0")) / Decimal(len(records))
            fill_values = [record["first_fill_ms"] for record in records if record["first_fill_ms"] is not None]
            final_values = [
                record["fill_to_finalize_ms"]
                for record in records
                if record["fill_to_finalize_ms"] is not None
            ]
            print(
                f"{label:18s} {len(records):5d} {wins:5d} "
                f"{(wins / len(records)) * 100:6.1f}% {float(avg_pnl):+10.5f} "
                f"{_fmt_metric(_med(fill_values),1):>11s} "
                f"{_fmt_metric(_med(final_values),1):>11s}"
            )

    print("\nSELECTIVE POST-FILL EDGE TRAJECTORY (Phase 1.8.2 samples)")
    trajectory: dict[int, list[dict[str, Decimal]]] = defaultdict(list)
    for record in selected:
        for key, sample in record["timeline"].items():
            if not isinstance(sample, dict):
                continue
            try:
                checkpoint = int(key)
            except Exception:
                checkpoint = int(_d(sample.get("target_elapsed_ms")))
            edge = sample.get("complete_now_net_edge_per_share")
            actual = sample.get("actual_elapsed_ms")
            if edge is None or actual is None:
                continue
            trajectory[checkpoint].append(
                {"edge": _d(edge), "actual": _d(actual)}
            )
    if not trajectory:
        print("no post-fill checkpoint samples yet; new Phase 1.8.2 events will populate this section")
    else:
        denominator = max(len(edge_records), 1)
        print(
            f"{'TARGET':>8s} {'N':>5s} {'OBS%':>7s} {'P50 ACTUAL':>11s} "
            f"{'P50 EDGE':>10s} {'EDGE>=0':>8s}"
        )
        print("-" * 58)
        for checkpoint in sorted(trajectory):
            samples = trajectory[checkpoint]
            actuals = [sample["actual"] for sample in samples]
            edges = [sample["edge"] for sample in samples]
            nonnegative = sum(1 for edge in edges if edge >= 0)
            print(
                f"{checkpoint:7d}ms {len(samples):5d} {(len(samples)/denominator)*100:6.1f}% "
                f"{_fmt_metric(_med(actuals),1):>11s} "
                f"{_fmt_metric(_med(edges),4):>10s} "
                f"{(nonnegative/len(samples))*100:7.1f}%"
            )

    print("\nSELECTIVE OUTCOME QUALITY")
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in selected:
        grouped[record["strategy"]].append(record)
    if not grouped:
        print("no selective outcomes")
    else:
        print(
            f"{'STRATEGY':24s} {'COMP_WIN':>8s} {'POS_NONCOMP':>11s} "
            f"{'NEG':>6s} {'FLAT':>6s} {'P&L':>10s}"
        )
        print("-" * 74)
        for strategy, records in sorted(grouped.items()):
            complete = sum(1 for record in records if record["true_win"])
            pos_noncomplete = sum(1 for record in records if record["positive_noncomplete"])
            negative = sum(1 for record in records if record["loss"])
            flat = sum(1 for record in records if record["flat"])
            pnl = sum((record["pnl"] for record in records), Decimal("0"))
            print(
                f"{strategy:24s} {complete:8d} {pos_noncomplete:11d} "
                f"{negative:6d} {flat:6d} {float(pnl):+10.4f}"
            )

    print("\nIDEAL ATOMIC BENCHMARK (excluded from shadow P&L)")
    if not result["atomic"]:
        print("no atomic captures")
    else:
        for strategy, item in sorted(result["atomic"].items()):
            if strategy_filter and strategy.upper() != strategy_filter:
                continue
            if asset_filter:
                captures = item["assets"].get(asset_filter, 0)
                pnl = item["asset_pnl"].get(asset_filter, Decimal("0"))
                assets = f"{asset_filter}:{captures}" if captures else ""
            else:
                captures = item["captures"]
                pnl = item["pnl"]
                assets = ",".join(
                    f"{key}:{value}" for key, value in sorted(item["assets"].items())
                )
            if not captures:
                continue
            print(
                f"{strategy:24s} captures={captures:5d} "
                f"benchmark_pnl={float(pnl):+.5f} assets={assets or '-'}"
            )

    print("\nATOMIC IDEAL-FILL TIMING")
    print("BUY/SELL variants are mirrored benchmark views; do not add them as independent opportunities.")
    timing_rows = 0
    for strategy, item in sorted(result["atomic"].items()):
        if strategy_filter and strategy.upper() != strategy_filter:
            continue
        lifetimes = (
            item["asset_lifetimes"].get(asset_filter, [])
            if asset_filter
            else item["lifetimes"]
        )
        if not lifetimes:
            continue
        timing_rows += 1
        p50 = _percentile(lifetimes, 0.50)
        p90 = _percentile(lifetimes, 0.90)
        max_life = max(lifetimes)
        survival = " ".join(
            f">={threshold}ms:{sum(1 for value in lifetimes if value >= Decimal(threshold))/len(lifetimes)*100:.0f}%"
            for threshold in ATOMIC_SURVIVAL_MS
        )
        print(
            f"{strategy:24s} closed={len(lifetimes):4d} "
            f"p50={float(p50 or 0):6.2f}ms p90={float(p90 or 0):6.2f}ms "
            f"max={float(max_life):7.2f}ms | {survival}"
        )
    if timing_rows == 0:
        print("no closed atomic lifetime samples")

    print("\nATOMIC LATENCY REPLAY (Phase 1.8.2 observed checkpoints)")
    replay_rows = 0
    for strategy, item in sorted(result["atomic"].items()):
        if strategy_filter and strategy.upper() != strategy_filter:
            continue
        instrumented = (
            item["instrumented_asset_captures"].get(asset_filter, 0)
            if asset_filter
            else item["instrumented_captures"]
        )
        if instrumented <= 0:
            continue
        for checkpoint, samples in sorted(item["latency_samples"].items()):
            filtered = [
                sample for sample in samples if not asset_filter or sample["asset"] == asset_filter
            ]
            if not filtered:
                continue
            replay_rows += 1
            actuals = [sample["actual_elapsed_ms"] for sample in filtered]
            edges = [sample["edge"] for sample in filtered]
            print(
                f"{strategy:24s} target={checkpoint:3d}ms "
                f"survived={len(filtered):3d}/{instrumented:<3d} "
                f"({len(filtered)/instrumented*100:5.1f}%) "
                f"actual_p50={float(_med(actuals) or 0):6.2f}ms "
                f"edge_p50={float(_med(edges) or 0):+.5f}/sh"
            )
    if replay_rows == 0:
        print("no Phase 1.8.2 latency replay samples yet")

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
