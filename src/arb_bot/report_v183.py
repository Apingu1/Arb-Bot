from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import sys
import tempfile
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from .report_v181 import _asset, _d, _fmt_metric, _med
from .report_v182 import build_compact_v182, cli as cli_v182


AGE_BUCKETS = (
    (Decimal("0"), Decimal("250"), "<250ms"),
    (Decimal("250"), Decimal("500"), "250-500ms"),
    (Decimal("500"), Decimal("1000"), "500-1000ms"),
    (Decimal("1000"), Decimal("2500"), "1-2.5s"),
    (Decimal("2500"), Decimal("5000"), "2.5-5s"),
    (Decimal("5000"), None, ">=5s"),
)
EDGE_BUCKETS = (
    (Decimal("0"), None, ">=0"),
    (Decimal("-0.005"), Decimal("0"), "-0.005..0"),
    (Decimal("-0.010"), Decimal("-0.005"), "-0.010..-0.005"),
    (Decimal("-0.015"), Decimal("-0.010"), "-0.015..-0.010"),
    (Decimal("-0.020"), Decimal("-0.015"), "-0.020..-0.015"),
    (None, Decimal("-0.020"), "< -0.020"),
)


def _iter_rows(path: Path):
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except Exception:
                continue
            if isinstance(row, dict) and isinstance(row.get("payload"), dict):
                yield row


def _latest_run_id(path: Path) -> str | None:
    latest = None
    for row in _iter_rows(path) or ():
        run_id = row["payload"].get("phase183_run_id")
        if run_id:
            latest = str(run_id)
    return latest


def _atomic_session_keys(path: Path, run_id: str) -> tuple[set[tuple[str, str, str]], set[tuple[str, str, str, str]]]:
    markets: set[tuple[str, str, str]] = set()
    captures: set[tuple[str, str, str, str]] = set()
    for row in _iter_rows(path) or ():
        if row.get("event_type") != "atomic_benchmark_capture_v183":
            continue
        payload = row["payload"]
        if str(payload.get("phase183_run_id") or "") != run_id:
            continue
        strategy = str(payload.get("strategy") or "")
        market_id = str(payload.get("market_id") or "")
        slug = str(payload.get("slug") or "")
        captured_at = str(payload.get("captured_at") or "")
        markets.add((strategy, market_id, slug))
        captures.add((strategy, market_id, slug, captured_at))
    return markets, captures


def _session_filtered_file(path: Path, run_id: str) -> Path:
    markets, captures = _atomic_session_keys(path, run_id)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".jsonl", delete=False
    )
    with handle:
        for row in _iter_rows(path) or ():
            payload = row["payload"]
            event_type = str(row.get("event_type") or "")
            keep = str(payload.get("phase183_run_id") or "") == run_id
            strategy = str(payload.get("strategy") or "")
            market_id = str(payload.get("market_id") or "")
            slug = str(payload.get("slug") or "")
            if not keep and event_type == "atomic_benchmark_capture":
                key = (strategy, market_id, slug, str(payload.get("captured_at") or ""))
                keep = key in captures
            elif not keep and event_type in {
                "atomic_benchmark_lifetime",
                "atomic_benchmark_latency_sample",
            }:
                keep = (strategy, market_id, slug) in markets
            if keep:
                handle.write(json.dumps(row, separators=(",", ":")) + "\n")
    return Path(handle.name)


def _age_bucket(value: Decimal) -> str:
    for lower, upper, label in AGE_BUCKETS:
        if value >= lower and (upper is None or value < upper):
            return label
    return ">=5s"


def _edge_bucket(value: Decimal) -> str:
    for lower, upper, label in EDGE_BUCKETS:
        if lower is None:
            if value < (upper or Decimal("0")):
                return label
        elif upper is None:
            if value >= lower:
                return label
        elif value >= lower and value < upper:
            return label
    return "< -0.020"


def _selective_records(result: dict[str, Any], strategy: str | None, asset: str | None):
    rows = []
    for record in result["selective_records"]:
        if strategy and record["strategy"].upper() != strategy:
            continue
        if asset and record["asset"] != asset:
            continue
        rows.append(record)
    return rows


def _corrected_trajectory_section(
    result: dict[str, Any], strategy: str | None, asset: str | None
) -> str:
    selected = _selective_records(result, strategy, asset)
    instrumented = [
        record
        for record in selected
        if record.get("fill_to_finalize_ms") is not None
    ]
    trajectory: dict[int, list[dict[str, Decimal]]] = defaultdict(list)
    for record in instrumented:
        for key, sample in (record.get("timeline") or {}).items():
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
            trajectory[checkpoint].append({"edge": _d(edge), "actual": _d(actual)})

    lines = [
        "SELECTIVE POST-FILL EDGE TRAJECTORY (instrumented cohort only)",
        f"denominator={len(instrumented)} instrumented first-fill outcomes; historical non-instrumented outcomes excluded",
    ]
    if not trajectory:
        lines.append("no post-fill checkpoint samples yet")
        return "\n".join(lines)
    lines.extend(
        [
            f"{'TARGET':>8s} {'N':>5s} {'OBS%':>7s} {'P50 ACTUAL':>11s} {'P50 EDGE':>10s} {'EDGE>=0':>8s}",
            "-" * 58,
        ]
    )
    denominator = max(len(instrumented), 1)
    for checkpoint in sorted(trajectory):
        samples = trajectory[checkpoint]
        actuals = [sample["actual"] for sample in samples]
        edges = [sample["edge"] for sample in samples]
        nonnegative = sum(1 for edge in edges if edge >= 0)
        lines.append(
            f"{checkpoint:7d}ms {len(samples):5d} {(len(samples)/denominator)*100:6.1f}% "
            f"{_fmt_metric(_med(actuals),1):>11s} {_fmt_metric(_med(edges),4):>10s} "
            f"{(nonnegative/len(samples))*100:7.1f}%"
        )
    return "\n".join(lines)


def _filter_event(payload: dict[str, Any], *, run_id: str | None, strategy: str | None, asset: str | None) -> bool:
    if run_id and str(payload.get("phase183_run_id") or "") != run_id:
        return False
    if strategy and str(payload.get("strategy") or "").upper() != strategy:
        return False
    if asset and _asset(payload) != asset:
        return False
    return True


def _counterfactual_table(path: Path, *, run_id: str | None, strategy: str | None, asset: str | None) -> str:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in _iter_rows(path) or ():
        if row.get("event_type") != "maker_variant_first_fill_counterfactual_v183":
            continue
        payload = row["payload"]
        if not _filter_event(payload, run_id=run_id, strategy=strategy, asset=asset):
            continue
        grouped[str(payload.get("strategy") or "UNKNOWN")].append(payload)

    lines = ["PHASE 1.8.3 FIRST-FILL ACTION COUNTERFACTUAL"]
    if not grouped:
        lines.append("no Phase 1.8.3 counterfactual outcomes yet")
        return "\n".join(lines)
    lines.extend(
        [
            f"{'STRATEGY':24s} {'N':>4s} {'BEST C/U/A':>12s} {'AVG ACT':>9s} {'AVG COMP':>9s} {'AVG UNW':>9s} {'AVG REGRET':>10s} {'2ND MKR':>8s}",
            "-" * 103,
        ]
    )
    for name, rows in sorted(grouped.items()):
        best = defaultdict(int)
        for item in rows:
            best[str(item.get("best_action") or "?")] += 1
        actual = [_d(item.get("actual_eventual_pnl")) for item in rows]
        complete = [
            _d(item.get("complete_now_total_pnl"))
            for item in rows
            if item.get("complete_now_total_pnl") is not None
        ]
        unwind = [
            _d(item.get("unwind_now_total_pnl"))
            for item in rows
            if item.get("unwind_now_total_pnl") is not None
        ]
        regret = [_d(item.get("actual_policy_regret")) for item in rows]
        second = sum(1 for item in rows if item.get("second_maker_fill_seen"))
        avg = lambda values: sum(values, Decimal("0")) / Decimal(len(values)) if values else Decimal("0")
        best_text = f"{best['COMPLETE_NOW']}/{best['UNWIND_NOW']}/{best['ACTUAL_POLICY']}"
        lines.append(
            f"{name:24s} {len(rows):4d} {best_text:>12s} "
            f"{float(avg(actual)):+9.5f} {float(avg(complete)):+9.5f} {float(avg(unwind)):+9.5f} "
            f"{float(avg(regret)):+10.5f} {(second/len(rows))*100:7.1f}%"
        )
    return "\n".join(lines)


def _second_maker_table(path: Path, *, run_id: str | None, strategy: str | None, asset: str | None) -> str:
    rows = []
    for row in _iter_rows(path) or ():
        if row.get("event_type") != "maker_variant_first_fill_counterfactual_v183":
            continue
        payload = row["payload"]
        if _filter_event(payload, run_id=run_id, strategy=strategy, asset=asset):
            rows.append(payload)
    lines = ["SECOND-MAKER-FILL TIMING"]
    if not rows:
        lines.append("no Phase 1.8.3 first-fill outcomes yet")
        return "\n".join(lines)
    seen = [item for item in rows if item.get("second_maker_fill_seen")]
    times = [
        _d(item.get("time_to_second_maker_fill_ms"))
        for item in seen
        if item.get("time_to_second_maker_fill_ms") is not None
    ]
    lines.append(
        f"first_fills={len(rows)} second_maker_seen={len(seen)} ({len(seen)/len(rows)*100:.1f}%) "
        f"p50_time={float(_med(times) or 0):.1f}ms"
    )
    return "\n".join(lines)


def _quote_age_edge_table(result: dict[str, Any], strategy: str | None, asset: str | None) -> str:
    selected = [
        record
        for record in _selective_records(result, strategy, asset)
        if record.get("edge_at_fill") is not None and record.get("first_fill_ms") is not None
    ]
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in selected:
        groups[(_age_bucket(record["first_fill_ms"]), _edge_bucket(record["edge_at_fill"]))].append(record)
    lines = ["QUOTE AGE × EDGE@FILL"]
    if not groups:
        lines.append("no joint quote-age/edge samples")
        return "\n".join(lines)
    lines.extend(
        [
            f"{'AGE':12s} {'EDGE':18s} {'N':>5s} {'WINS':>5s} {'WIN%':>7s} {'AVG PNL':>10s}",
            "-" * 64,
        ]
    )
    age_order = [item[2] for item in AGE_BUCKETS]
    edge_order = [item[2] for item in EDGE_BUCKETS]
    for age_label in age_order:
        for edge_label in edge_order:
            rows = groups.get((age_label, edge_label), [])
            if not rows:
                continue
            wins = sum(1 for item in rows if item["true_win"])
            avg_pnl = sum((item["pnl"] for item in rows), Decimal("0")) / Decimal(len(rows))
            lines.append(
                f"{age_label:12s} {edge_label:18s} {len(rows):5d} {wins:5d} "
                f"{wins/len(rows)*100:6.1f}% {float(avg_pnl):+10.5f}"
            )
    return "\n".join(lines)


def _ghost_gate_table(path: Path, *, run_id: str | None, strategy: str | None, asset: str | None) -> str:
    agg: dict[tuple[str, str, int], dict[str, Any]] = defaultdict(
        lambda: {"n": 0, "triggered": 0, "avoided": 0, "delta": Decimal("0"), "cf": Decimal("0")}
    )
    for row in _iter_rows(path) or ():
        if row.get("event_type") != "maker_variant_ghost_prefill_gate_result_v183":
            continue
        payload = row["payload"]
        if not _filter_event(payload, run_id=run_id, strategy=strategy, asset=asset):
            continue
        results = payload.get("results") or {}
        for threshold, by_scope in results.items():
            for scope, scope_row in (by_scope or {}).items():
                for latency, outcome in (scope_row.get("latencies") or {}).items():
                    key = (scope, str(threshold), int(latency))
                    item = agg[key]
                    item["n"] += 1
                    item["triggered"] += int(bool(scope_row.get("triggered")))
                    item["avoided"] += int(bool(outcome.get("would_avoid_first_fill")))
                    item["delta"] += _d(outcome.get("delta_vs_actual"))
                    item["cf"] += _d(outcome.get("counterfactual_pnl"))

    lines = ["GHOST PRE-FILL CANCEL GATE (no execution behaviour changed)"]
    if not agg:
        lines.append("no Phase 1.8.3 ghost-gate outcomes yet")
        return "\n".join(lines)
    lines.extend(
        [
            f"{'SCOPE':17s} {'EDGE<=':>8s} {'LAT':>5s} {'N':>5s} {'TRIG':>5s} {'AVOID':>6s} {'CF PNL':>10s} {'DELTA':>10s}",
            "-" * 77,
        ]
    )
    def sort_key(item):
        (scope, threshold, latency), _ = item
        return (scope, Decimal(threshold), latency)
    for (scope, threshold, latency), item in sorted(agg.items(), key=sort_key, reverse=False):
        lines.append(
            f"{scope:17s} {threshold:>8s} {latency:4d}ms {item['n']:5d} {item['triggered']:5d} "
            f"{item['avoided']:6d} {float(item['cf']):+10.4f} {float(item['delta']):+10.4f}"
        )
    return "\n".join(lines)


def _atomic_outcome_table(path: Path, *, run_id: str | None, strategy: str | None, asset: str | None) -> str:
    grouped: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in _iter_rows(path) or ():
        if row.get("event_type") != "atomic_benchmark_latency_outcome_v183":
            continue
        payload = row["payload"]
        if not _filter_event(payload, run_id=run_id, strategy=strategy, asset=asset):
            continue
        grouped[(str(payload.get("strategy") or "ATOMIC"), int(payload.get("target_latency_ms") or 0))].append(payload)

    lines = ["ATOMIC CHECKPOINT OUTCOMES V1.8.3"]
    lines.append("S=survived, E=expired before checkpoint, M=scheduler missed; BUY/SELL remain mirrored views.")
    if not grouped:
        lines.append("no Phase 1.8.3 atomic checkpoint outcomes yet")
        return "\n".join(lines)
    lines.extend(
        [
            f"{'STRATEGY':24s} {'LAT':>5s} {'S/E/M':>13s} {'SURV%':>7s} {'MISS%':>7s} {'EDGE P50':>10s} {'PNL P50':>10s}",
            "-" * 93,
        ]
    )
    for (name, checkpoint), rows in sorted(grouped.items()):
        s = [item for item in rows if item.get("outcome") == "SURVIVED"]
        e = sum(1 for item in rows if item.get("outcome") == "EXPIRED_BEFORE_CHECKPOINT")
        m = sum(1 for item in rows if item.get("outcome") == "SCHEDULER_MISSED_CHECKPOINT")
        edges = [_d(item.get("edge_per_share")) for item in s if item.get("edge_per_share") is not None]
        pnls = [_d(item.get("pnl")) for item in s if item.get("pnl") is not None]
        total = len(rows)
        lines.append(
            f"{name:24s} {checkpoint:4d}ms {len(s):3d}/{e:3d}/{m:<3d} "
            f"{len(s)/total*100:6.1f}% {m/total*100:6.1f}% "
            f"{float(_med(edges) or 0):+10.5f} {float(_med(pnls) or 0):+10.5f}"
        )
    return "\n".join(lines)


def cli() -> None:
    parser = argparse.ArgumentParser(
        description="Phase 1.8.3 counterfactual shadow-arbitrage report"
    )
    parser.add_argument("path", nargs="?", default="data/shadow_events.jsonl")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--asset", default=None)
    parser.add_argument("--strategy", default=None)
    parser.add_argument(
        "--session",
        nargs="?",
        const="latest",
        default=None,
        help="Restrict to one Phase 1.8.3 run id; omit value for latest run",
    )
    args = parser.parse_args()

    original_path = Path(args.path)
    strategy_filter = args.strategy.upper() if args.strategy else None
    asset_filter = args.asset.upper() if args.asset else None
    run_id = None
    report_path = original_path
    temp_path: Path | None = None

    if args.session:
        run_id = _latest_run_id(original_path) if args.session == "latest" else args.session
        if run_id is None:
            print("No Phase 1.8.3 run id found in the report file.")
            return
        temp_path = _session_filtered_file(original_path, run_id)
        report_path = temp_path

    # Reuse the accepted 1.8.2 report so all historical tables remain intact,
    # then replace only the known bad trajectory denominator and append 1.8.3
    # counterfactual sections.
    old_argv = sys.argv[:]
    base_argv = [old_argv[0], str(report_path)]
    if args.all:
        base_argv.append("--all")
    if args.asset:
        base_argv.extend(["--asset", args.asset])
    if args.strategy:
        base_argv.extend(["--strategy", args.strategy])
    output = io.StringIO()
    try:
        sys.argv = base_argv
        with contextlib.redirect_stdout(output):
            cli_v182()
    finally:
        sys.argv = old_argv

    result = build_compact_v182(report_path)
    corrected = _corrected_trajectory_section(result, strategy_filter, asset_filter)
    text = output.getvalue()
    text = re.sub(
        r"SELECTIVE POST-FILL EDGE TRAJECTORY \(Phase 1\.8\.2 samples\).*?(?=\nSELECTIVE OUTCOME QUALITY)",
        corrected + "\n",
        text,
        flags=re.S,
    )
    if run_id:
        text = f"PHASE 1.8.3 SESSION FILTER // run_id={run_id}\n" + text
    print(text.rstrip())

    extra_source = original_path
    print()
    print(_quote_age_edge_table(result, strategy_filter, asset_filter))
    print()
    print(_counterfactual_table(extra_source, run_id=run_id, strategy=strategy_filter, asset=asset_filter))
    print()
    print(_second_maker_table(extra_source, run_id=run_id, strategy=strategy_filter, asset=asset_filter))
    print()
    print(_ghost_gate_table(extra_source, run_id=run_id, strategy=strategy_filter, asset=asset_filter))
    print()
    print(_atomic_outcome_table(extra_source, run_id=run_id, strategy=strategy_filter, asset=asset_filter))
    print()
    print(
        "PHASE 1.8 CONTROL NOTE // legacy TAKER, HEDGE, EV, DFOK and RFOK families are intentionally "
        "disabled by SettingsV18 defaults; zero activity in those heartbeat rows is not evidence that their gates rejected the market."
    )

    if temp_path is not None:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass


if __name__ == "__main__":
    cli()
