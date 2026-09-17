from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any

from .config_v1810 import SettingsV1810
from .report_v1810_cli import CORE_STRATEGIES, EXECUTION_EVENTS, ZERO, _d, _regime, _timestamp


WINDOWS: tuple[tuple[str, float], ...] = (
    ("500ms", 0.5),
    ("5s", 5.0),
    ("30s", 30.0),
    ("60s", 60.0),
)
MAX_MISS_LOSS_SHARE = Decimal("0.25")


@dataclass(slots=True)
class ScanResult:
    rows: list[dict[str, Any]]
    spans: dict[str, tuple[float, float]]
    sources: list[Path]
    lines: int
    malformed: int


def _sources(source: Path) -> list[Path]:
    if source.is_dir():
        return sorted(path for path in source.glob("phase1810_*.jsonl") if path.is_file())
    return [source] if source.is_file() else []


def _scan(source: Path, asset_filter: str | None) -> ScanResult:
    """Read every compact archive once and retain only report-relevant rows."""

    sources = _sources(source)
    rows: list[dict[str, Any]] = []
    span_values: dict[str, list[float]] = defaultdict(list)
    line_count = 0
    malformed = 0

    for path in sources:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line_count += 1
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    malformed += 1
                    continue
                if not isinstance(row, dict) or not isinstance(row.get("payload"), dict):
                    malformed += 1
                    continue
                payload = row["payload"]
                run_id = str(payload.get("phase1810_run_id") or "")
                if not run_id:
                    continue
                ts = _timestamp(payload)
                if ts is not None:
                    span_values[run_id].append(ts)
                event_type = str(row.get("event_type") or "")
                if event_type not in EXECUTION_EVENTS | {"raw_positive_observation_v189"}:
                    continue
                asset = str(payload.get("asset") or "UNKNOWN").upper()
                if asset_filter and asset != asset_filter:
                    continue
                rows.append(row)

    spans = {
        run_id: (min(values), max(values))
        for run_id, values in span_values.items()
        if values
    }
    return ScanResult(rows, spans, sources, line_count, malformed)


def _empty_episode(payload: dict[str, Any], episode_id: str) -> dict[str, Any]:
    return {
        "id": episode_id,
        "run": str(payload.get("phase1810_run_id") or "UNKNOWN"),
        "market": str(payload.get("market_id") or "UNKNOWN"),
        "asset": str(payload.get("asset") or "UNKNOWN").upper(),
        "times": [],
        "raw": [],
        "executions": defaultdict(list),
        "base_ids": {episode_id},
    }


def _base_episodes(rows: list[dict[str, Any]], settings: SettingsV1810) -> list[dict[str, Any]]:
    episodes: dict[tuple[str, str], dict[str, Any]] = {}
    missing_sequence = 0
    for row in rows:
        payload = row["payload"]
        run_id = str(payload.get("phase1810_run_id") or "UNKNOWN")
        episode_id = str(payload.get("market_episode_id") or "")
        if not episode_id:
            missing_sequence += 1
            episode_id = f"MISSING-{missing_sequence:08d}"
        key = (run_id, episode_id)
        episode = episodes.setdefault(key, _empty_episode(payload, episode_id))
        ts = _timestamp(payload)
        if ts is not None:
            episode["times"].append(ts)
        if row["event_type"] == "raw_positive_observation_v189":
            episode["raw"].append(payload)
        else:
            strategy = str(payload.get("strategy") or "")
            if strategy in CORE_STRATEGIES:
                episode["executions"][strategy].append((ts or 0.0, payload))

    result = list(episodes.values())
    for episode in result:
        episode["regime"] = _regime(episode["raw"], settings)
        episode["start"] = min(episode["times"]) if episode["times"] else 0.0
        episode["end"] = max(episode["times"]) if episode["times"] else episode["start"]
    return result


def _clone_episode(episode: dict[str, Any], merged_id: str) -> dict[str, Any]:
    return {
        "id": merged_id,
        "run": episode["run"],
        "market": episode["market"],
        "asset": episode["asset"],
        "times": list(episode["times"]),
        "raw": list(episode["raw"]),
        "executions": defaultdict(
            list,
            {name: list(values) for name, values in episode["executions"].items()},
        ),
        "base_ids": set(episode["base_ids"]),
        "start": episode["start"],
        "end": episode["end"],
    }


def _merge_windows(
    base_episodes: list[dict[str, Any]], gap_seconds: float, settings: SettingsV1810
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for episode in base_episodes:
        grouped[(episode["run"], episode["market"])].append(episode)

    merged: list[dict[str, Any]] = []
    sequence = 0
    for (run_id, market), values in sorted(grouped.items()):
        current: dict[str, Any] | None = None
        for episode in sorted(values, key=lambda item: (item["start"], item["id"])):
            if current is None or episode["start"] - current["end"] > gap_seconds:
                sequence += 1
                current = _clone_episode(episode, f"{run_id}:{market}:M{sequence:06d}")
                merged.append(current)
                continue
            current["times"].extend(episode["times"])
            current["raw"].extend(episode["raw"])
            current["base_ids"].update(episode["base_ids"])
            current["start"] = min(current["start"], episode["start"])
            current["end"] = max(current["end"], episode["end"])
            if current["asset"] == "UNKNOWN" and episode["asset"] != "UNKNOWN":
                current["asset"] = episode["asset"]
            for strategy, executions in episode["executions"].items():
                current["executions"][strategy].extend(executions)

    for episode in merged:
        episode["regime"] = _regime(episode["raw"], settings)
    return merged


def _first_execution(episode: dict[str, Any], strategy: str) -> tuple[float, dict[str, Any]] | None:
    executions = episode["executions"].get(strategy, [])
    return min(executions, key=lambda item: item[0]) if executions else None


def _metrics(episodes: list[dict[str, Any]], strategy: str) -> dict[str, Any]:
    raw_pnl = ZERO
    fills = 0
    values: list[Decimal] = []
    selected: list[tuple[dict[str, Any], dict[str, Any], Decimal]] = []
    for episode in episodes:
        executions = episode["executions"].get(strategy, [])
        if not executions:
            continue
        fills += len(executions)
        raw_pnl += sum((_d(payload.get("realized_pnl")) for _, payload in executions), ZERO)
        first = _first_execution(episode, strategy)
        if first is None:
            continue
        pnl = _d(first[1].get("realized_pnl"))
        values.append(pnl)
        selected.append((episode, first[1], pnl))
    total = sum(values, ZERO)
    ordered = sorted(values, reverse=True)
    best = ordered[0] if ordered else ZERO
    return {
        "fills": fills,
        "raw_pnl": raw_pnl,
        "values": values,
        "selected": selected,
        "total": total,
        "ex_best": total - best if values else ZERO,
        "top1": best / total * 100 if total > ZERO else ZERO,
        "top3": sum(ordered[:3], ZERO) / total * 100 if total > ZERO else ZERO,
        "median": median(values) if values else ZERO,
        "worst": min(values) if values else ZERO,
    }


def _window_summary(window_episodes: dict[str, list[dict[str, Any]]]) -> list[str]:
    lines = [
        "MULTI-WINDOW EPISODE SUMMARY",
        "Runs never merge. Within each run and market, a new macro episode starts only after the stated quiet gap.",
        f"{'GAP':7s} {'RAW EP':>7s} {'EXEC EP':>8s} {'BFOK10 EP':>9s} {'BFOK10 PNL':>12s} {'EX BEST':>12s}",
        "-" * 62,
    ]
    for label, _ in WINDOWS:
        episodes = window_episodes[label]
        raw_count = sum(1 for episode in episodes if episode["raw"])
        exec_count = sum(1 for episode in episodes if episode["executions"])
        stat = _metrics(episodes, "BFOK-10")
        lines.append(
            f"{label:7s} {raw_count:7d} {exec_count:8d} {len(stat['values']):9d} "
            f"{float(stat['total']):+12.5f} {float(stat['ex_best']):+12.5f}"
        )
    return lines


def _model_windows(window_episodes: dict[str, list[dict[str, Any]]]) -> list[str]:
    lines = [
        "CONSERVATIVE MODEL RESULTS — FIRST EXECUTION PER MACRO EPISODE",
        f"{'GAP':7s} {'STRATEGY':12s} {'FILLS':>6s} {'EP':>5s} {'RAW PNL':>11s} {'1/EP PNL':>11s} {'EX BEST':>11s} {'TOP1':>7s} {'TOP3':>7s} {'WORST':>10s}",
        "-" * 105,
    ]
    any_rows = False
    for label, _ in WINDOWS:
        for strategy in CORE_STRATEGIES:
            stat = _metrics(window_episodes[label], strategy)
            if not stat["values"]:
                continue
            any_rows = True
            lines.append(
                f"{label:7s} {strategy:12s} {stat['fills']:6d} {len(stat['values']):5d} "
                f"{float(stat['raw_pnl']):+11.5f} {float(stat['total']):+11.5f} {float(stat['ex_best']):+11.5f} "
                f"{float(stat['top1']):6.1f}% {float(stat['top3']):6.1f}% {float(stat['worst']):+10.5f}"
            )
    if not any_rows:
        lines.append("no Phase 1.8.10 executable episodes yet")
    return lines


def _breakdowns(episodes: list[dict[str, Any]]) -> list[str]:
    lines = [
        "30-SECOND MACRO VIEW — ASSET × STRATEGY",
        f"{'STRATEGY':12s} {'ASSET':>8s} {'EP':>5s} {'PNL':>11s} {'EX BEST':>11s} {'WORST':>10s}",
        "-" * 62,
    ]
    any_asset = False
    for strategy in CORE_STRATEGIES:
        selected = _metrics(episodes, strategy)["selected"]
        by_asset: dict[str, list[Decimal]] = defaultdict(list)
        for episode, _, pnl in selected:
            by_asset[episode["asset"]].append(pnl)
        for asset in sorted(by_asset):
            values = by_asset[asset]
            total = sum(values, ZERO)
            any_asset = True
            lines.append(
                f"{strategy:12s} {asset:>8s} {len(values):5d} {float(total):+11.5f} "
                f"{float(total-max(values)):+11.5f} {float(min(values)):+10.5f}"
            )
    if not any_asset:
        lines.append("no asset-level executable episodes yet")

    lines.extend([
        "",
        "30-SECOND MACRO VIEW — REGIME × STRATEGY",
        f"{'STRATEGY':12s} {'REGIME':14s} {'EP':>5s} {'PNL':>11s} {'WORST':>10s}",
        "-" * 58,
    ])
    any_regime = False
    for strategy in CORE_STRATEGIES:
        selected = _metrics(episodes, strategy)["selected"]
        by_regime: dict[str, list[Decimal]] = defaultdict(list)
        for episode, _, pnl in selected:
            by_regime[episode["regime"]].append(pnl)
        for regime in ("NORMAL", "MODERATE", "MAJOR_SURGE", "UNKNOWN"):
            values = by_regime.get(regime, [])
            if not values:
                continue
            any_regime = True
            lines.append(
                f"{strategy:12s} {regime:14s} {len(values):5d} "
                f"{float(sum(values, ZERO)):+11.5f} {float(min(values)):+10.5f}"
            )
    if not any_regime:
        lines.append("no regime-level executable episodes yet")
    return lines


def _gate(label: str, passed: bool, detail: str) -> str:
    return f"[{'PASS' if passed else 'FAIL'}] {label}: {detail}"


def _decision(window_episodes: dict[str, list[dict[str, Any]]]) -> list[str]:
    base = window_episodes["500ms"]
    raw_count = sum(1 for episode in base if episode["raw"])
    bfok = _metrics(base, "BFOK-10")
    bfok_30 = _metrics(window_episodes["30s"], "BFOK-10")
    bfok_60 = _metrics(window_episodes["60s"], "BFOK-10")

    miss_count = 0
    miss_loss = ZERO
    for _, payload, pnl in bfok["selected"]:
        if str(payload.get("status") or "") == "ONE_LEG_MISS":
            miss_count += 1
            if pnl < ZERO:
                miss_loss += -pnl
    gross_wins = sum((value for value in bfok["values"] if value > ZERO), ZERO)
    miss_share = miss_loss / gross_wins if gross_wins > ZERO else (Decimal("1") if miss_loss else ZERO)

    checks = [
        ("RAW sample", raw_count >= 50, f"{raw_count}/50 independent 500ms episodes"),
        ("BFOK-10 sample", len(bfok["values"]) >= 25, f"{len(bfok['values'])}/25 realistic episodes"),
        ("BFOK-10 ex-best", bool(bfok["values"]) and bfok["ex_best"] > ZERO, f"500ms P&L {float(bfok['ex_best']):+.5f}"),
        ("30s surge-deduped ex-best", bool(bfok_30["values"]) and bfok_30["ex_best"] > ZERO, f"P&L {float(bfok_30['ex_best']):+.5f}"),
        ("60s surge-deduped ex-best", bool(bfok_60["values"]) and bfok_60["ex_best"] > ZERO, f"P&L {float(bfok_60['ex_best']):+.5f}"),
        (
            "BFOK-10 miss-loss budget",
            bool(bfok["values"]) and miss_share <= MAX_MISS_LOSS_SHARE,
            f"{miss_count} first-trade misses, loss={float(miss_loss):.5f}, "
            f"{float(miss_share * 100):.1f}% of gross wins (limit {float(MAX_MISS_LOSS_SHARE * 100):.0f}%)",
        ),
    ]
    passed = sum(1 for _, ok, _ in checks if ok)
    lines = [
        "VALIDATION GATES",
        *(_gate(label, ok, detail) for label, ok, detail in checks),
        "",
        f"OVERALL: {'READY FOR PHASE 1.8.11 DESIGN' if passed == len(checks) else 'NOT READY'} ({passed}/{len(checks)} gates passed)",
        "The miss-loss budget is a reporting rule: first-trade BFOK-10 miss losses may consume at most 25% of gross winning P&L.",
        "Passing these shadow gates does not authorize live trading.",
    ]
    return lines


def cumulative_report(source: Path, asset_filter: str | None = None) -> str:
    settings = SettingsV1810()
    scan = _scan(source, asset_filter)
    base = _base_episodes(scan.rows, settings)
    window_episodes = {
        label: _merge_windows(base, seconds, settings)
        for label, seconds in WINDOWS
    }
    active_seconds = sum(max(0.0, end - start) for start, end in scan.spans.values())
    hours = active_seconds / 3600
    raw_count = sum(1 for episode in window_episodes["500ms"] if episode["raw"])
    rate = raw_count / hours if hours > 0 else 0.0
    source_text = str(source)

    lines = [
        "PHASE 1.8.10A FAST CUMULATIVE VALIDATION",
        "Compact Phase 1.8.10 archives are scanned once; every window below is then calculated in memory.",
        f"source={source_text} files={len(scan.sources)} lines={scan.lines} malformed={scan.malformed} "
        f"runs={len(scan.spans)} observed_hours={hours:.3f} raw_500ms_rate_per_hour={rate:.2f}",
    ]
    if scan.sources:
        lines.append("archives=" + ", ".join(path.name for path in scan.sources))
    else:
        lines.append("archives=none (no phase1810_*.jsonl files found)")
    if asset_filter:
        lines.append(f"asset_filter={asset_filter}")

    for section in (
        _window_summary(window_episodes),
        _model_windows(window_episodes),
        _breakdowns(window_episodes["30s"]),
        _decision(window_episodes),
    ):
        lines.extend(["", *section])

    lines.extend([
        "",
        "INTERPRETATION",
        "RAW P&L is repeated shadow-book activity and must not be treated as account profit. 1/EP P&L permits only the first execution of each strategy in each episode.",
        "The 30s and 60s views test whether several short bursts were really one wider market surge. Strategies are alternatives and their P&L must not be added together.",
        f"generated_at={datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}",
    ])
    return "\n".join(lines)
