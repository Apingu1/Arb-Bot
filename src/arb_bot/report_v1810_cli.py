from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any

from . import report_v189_cli as _base
from .config_v1810 import SettingsV1810
from .report_v183 import _iter_rows
from .report_v186_cli import _fast_session_rewrite


ZERO = Decimal("0")
EXECUTION_EVENTS = {
    "dual_fok_execution_summary",
    "batch_fok_execution_summary_v187",
}
CORE_STRATEGIES = ("PFOK", "PFOK-S10", "PFOK-S20", "BFOK-1", "BFOK-5", "BFOK-10", "BFOK-20", "BFOK-EV")


def _d(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except Exception:
        return ZERO


def _timestamp(payload: dict[str, Any]) -> float | None:
    value = (
        payload.get("observed_at")
        or payload.get("finalized_at")
        or payload.get("detected_at")
        or payload.get("recorded_at")
    )
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _regime(raw_rows: list[dict[str, Any]], settings: SettingsV1810) -> str:
    if not raw_rows:
        return "UNKNOWN"
    active = any(bool(row.get("surge_active")) for row in raw_rows)
    move_1s = max((_d(row.get("surge_move_1s")) for row in raw_rows), default=ZERO)
    move_3s = max((_d(row.get("surge_move_3s")) for row in raw_rows), default=ZERO)
    update_rate = max((int(row.get("surge_updates_per_second") or 0) for row in raw_rows), default=0)
    if (
        active
        or move_1s >= settings.surge_move_1s
        or move_3s >= settings.surge_move_3s
        or update_rate >= settings.surge_updates_per_second
    ):
        return "MAJOR_SURGE"
    if (
        move_1s >= settings.surge_move_1s / 2
        or move_3s >= settings.surge_move_3s / 2
        or update_rate >= max(1, settings.surge_updates_per_second // 2)
    ):
        return "MODERATE"
    return "NORMAL"


def _phase_rows(path: Path, asset_filter: str | None) -> tuple[list[dict[str, Any]], dict[str, tuple[float, float]]]:
    selected: list[dict[str, Any]] = []
    spans: dict[str, list[float]] = defaultdict(list)
    for row in _iter_rows(path) or ():
        payload = row.get("payload", {})
        run_id = str(payload.get("phase1810_run_id") or "")
        if not run_id:
            continue
        ts = _timestamp(payload)
        if ts is not None:
            spans[run_id].append(ts)
        event_type = str(row.get("event_type") or "")
        if event_type not in EXECUTION_EVENTS | {"raw_positive_observation_v189"}:
            continue
        asset = str(payload.get("asset") or "UNKNOWN").upper()
        if asset_filter and asset != asset_filter:
            continue
        selected.append(row)
    compact_spans = {
        run_id: (min(values), max(values))
        for run_id, values in spans.items()
        if values
    }
    return selected, compact_spans


def _episode_table(path: Path, asset_filter: str | None) -> str:
    settings = SettingsV1810()
    rows, spans = _phase_rows(path, asset_filter)
    episodes: dict[str, dict[str, Any]] = {}
    for row in rows:
        payload = row["payload"]
        episode_id = str(payload.get("market_episode_id") or "")
        if not episode_id:
            continue
        episode = episodes.setdefault(
            episode_id,
            {
                "id": episode_id,
                "market": str(payload.get("market_id") or "UNKNOWN"),
                "asset": str(payload.get("asset") or "UNKNOWN").upper(),
                "times": [],
                "raw": [],
                "executions": defaultdict(list),
            },
        )
        ts = _timestamp(payload)
        if ts is not None:
            episode["times"].append(ts)
        if row["event_type"] == "raw_positive_observation_v189":
            episode["raw"].append(payload)
        else:
            strategy = str(payload.get("strategy") or "")
            if strategy in CORE_STRATEGIES:
                episode["executions"][strategy].append((ts or 0.0, payload))

    for episode in episodes.values():
        episode["regime"] = _regime(episode["raw"], settings)

    active_seconds = sum(max(0.0, end - start) for start, end in spans.values())
    hours = active_seconds / 3600
    raw_episode_count = sum(1 for episode in episodes.values() if episode["raw"])
    executable_episode_count = sum(1 for episode in episodes.values() if episode["executions"])

    lines = [
        "PHASE 1.8.10 SURGE CONCENTRATION & REAL CAPACITY",
        "EPISODE = shared market burst across all variants; conservative P&L permits the first execution per strategy per episode only.",
        f"runs={len(spans)} observed_hours={hours:.3f} raw_positive_episodes={raw_episode_count} "
        f"executable_episodes={executable_episode_count} raw_episode_rate_per_hour={(raw_episode_count / hours if hours > 0 else 0):.2f}",
        "",
        f"{'STRATEGY':12s} {'FILLS':>6s} {'EP':>5s} {'RAW PNL':>11s} {'1/EP PNL':>11s} {'EX BEST':>11s} {'TOP1':>7s} {'TOP3':>7s} {'MED EP':>10s} {'WORST':>10s}",
        "-" * 106,
    ]

    any_strategy = False
    first_pnl_by_strategy: dict[str, dict[str, Decimal]] = defaultdict(dict)
    for strategy in CORE_STRATEGIES:
        raw_pnl = ZERO
        fills = 0
        episode_pnls: list[Decimal] = []
        for episode_id, episode in episodes.items():
            executions = sorted(episode["executions"].get(strategy, []), key=lambda item: item[0])
            if not executions:
                continue
            fills += len(executions)
            raw_pnl += sum((_d(payload.get("realized_pnl")) for _, payload in executions), ZERO)
            first_pnl = _d(executions[0][1].get("realized_pnl"))
            first_pnl_by_strategy[strategy][episode_id] = first_pnl
            episode_pnls.append(first_pnl)
        if not episode_pnls:
            continue
        any_strategy = True
        total = sum(episode_pnls, ZERO)
        ordered = sorted(episode_pnls, reverse=True)
        best = ordered[0]
        top1 = best / total * 100 if total > ZERO else ZERO
        top3 = sum(ordered[:3], ZERO) / total * 100 if total > ZERO else ZERO
        lines.append(
            f"{strategy:12s} {fills:6d} {len(episode_pnls):5d} {float(raw_pnl):+11.5f} {float(total):+11.5f} "
            f"{float(total-best):+11.5f} {float(top1):6.1f}% {float(top3):6.1f}% "
            f"{float(median(episode_pnls)):+10.5f} {float(min(episode_pnls)):+10.5f}"
        )
    if not any_strategy:
        lines.append("no Phase 1.8.10 executable episodes yet")

    lines.extend([
        "",
        "REGIME × STRATEGY — FIRST EXECUTION PER EPISODE",
        f"{'STRATEGY':12s} {'REGIME':14s} {'EP':>5s} {'PNL':>11s}",
        "-" * 46,
    ])
    any_regime = False
    for strategy in CORE_STRATEGIES:
        for regime in ("NORMAL", "MODERATE", "MAJOR_SURGE", "UNKNOWN"):
            values = [
                pnl
                for episode_id, pnl in first_pnl_by_strategy.get(strategy, {}).items()
                if episodes[episode_id]["regime"] == regime
            ]
            if values:
                any_regime = True
                lines.append(f"{strategy:12s} {regime:14s} {len(values):5d} {float(sum(values, ZERO)):+11.5f}")
    if not any_regime:
        lines.append("no classified executable episodes yet")

    lines.extend([
        "",
        "INDEPENDENT EPISODE LEDGER — HIGHEST BFOK-10 P&L FIRST",
        f"{'EPISODE':29s} {'START UTC':19s} {'ASSET':>6s} {'REGIME':14s} {'DUR MS':>8s} {'RAW':>6s} {'PEAK EDGE':>10s} {'BFOK10':>10s}",
        "-" * 113,
    ])
    ledger = []
    for episode_id, episode in episodes.items():
        times = episode["times"]
        start = min(times) if times else 0.0
        duration_ms = (max(times) - start) * 1000 if times else 0.0
        peak_edge = max(
            (_d(payload.get("fee_adjusted_edge_per_share")) for payload in episode["raw"]),
            default=ZERO,
        )
        executions = sorted(episode["executions"].get("BFOK-10", []), key=lambda item: item[0])
        bfok10 = _d(executions[0][1].get("realized_pnl")) if executions else None
        ledger.append((bfok10 if bfok10 is not None else Decimal("-999999"), peak_edge, episode_id, episode, start, duration_ms, bfok10))
    if ledger:
        for _, peak_edge, episode_id, episode, start, duration_ms, bfok10 in sorted(ledger, reverse=True)[:20]:
            start_text = datetime.fromtimestamp(start, timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if start else "-"
            pnl_text = "-" if bfok10 is None else f"{float(bfok10):+.5f}"
            lines.append(
                f"{episode_id:29.29s} {start_text:19s} {episode['asset']:>6s} {episode['regime']:14s} "
                f"{duration_ms:8.1f} {len(episode['raw']):6d} {float(peak_edge):+10.5f} {pnl_text:>10s}"
            )
    else:
        lines.append("no independent episodes yet")

    lines.extend([
        "",
        "RAW EPISODE CAPACITY PROXY",
        "A refresh slot requires both outcome books to have newer exchange revisions. It is stricter than repeated observations but remains shadow evidence, not a live-fill guarantee.",
        f"{'ASSET':8s} {'EP':>5s} {'POS OBS':>8s} {'1/EP SLOTS':>10s} {'DUAL-REFRESH':>13s} {'PEAK EDGE':>11s}",
        "-" * 63,
    ])
    assets: dict[str, dict[str, Any]] = defaultdict(lambda: {"episodes": 0, "obs": 0, "slots": 0, "refresh": 0, "peak": ZERO})
    for episode in episodes.values():
        raw = sorted(episode["raw"], key=lambda payload: _timestamp(payload) or 0.0)
        if not raw:
            continue
        stat = assets[episode["asset"]]
        stat["episodes"] += 1
        stat["obs"] += len(raw)
        stat["slots"] += 1
        stat["peak"] = max(stat["peak"], max((_d(payload.get("fee_adjusted_edge_per_share")) for payload in raw), default=ZERO))
        last_a = int(raw[0].get("book_revision_a") or 0)
        last_b = int(raw[0].get("book_revision_b") or 0)
        for payload in raw[1:]:
            rev_a = int(payload.get("book_revision_a") or 0)
            rev_b = int(payload.get("book_revision_b") or 0)
            if rev_a > last_a and rev_b > last_b:
                stat["refresh"] += 1
                last_a, last_b = rev_a, rev_b
    if assets:
        for asset in sorted(assets):
            stat = assets[asset]
            lines.append(
                f"{asset:8s} {stat['episodes']:5d} {stat['obs']:8d} {stat['slots']:10d} {stat['refresh']:13d} {float(stat['peak']):+11.5f}"
            )
    else:
        lines.append("no Phase 1.8.10 RAW episodes yet")

    lines.extend([
        "",
        "DECISION CHECK",
        "Require >=50 RAW episodes, >=25 realistic BFOK-10 episodes, positive BFOK-10 P&L excluding its best episode, and acceptable miss losses before live-readiness work.",
        "The inherited legacy headline may still include older unlabelled rows when reporting a mixed historical file; this Phase 1.8.10 table is authoritative for run-aware independence.",
    ])
    return "\n".join(lines)


def cli() -> None:
    original_argv = list(sys.argv)
    if "--phase1810-cumulative" in original_argv:
        parser = argparse.ArgumentParser(
            description="Fast one-pass cumulative Phase 1.8.10 validation report"
        )
        parser.add_argument(
            "--phase1810-cumulative",
            nargs="?",
            const=SettingsV1810().v1810_session_archive_dir,
            required=True,
            metavar="PATH",
            help="Compact archive directory or a previously concatenated compact JSONL file",
        )
        parser.add_argument("--asset", default=None)
        parser.add_argument(
            "--all",
            action="store_true",
            help="Accepted for compatibility; cumulative mode already includes every Phase 1.8.10 section",
        )
        args = parser.parse_args(original_argv[1:])
        from .report_v1810a import cumulative_report

        asset_filter = args.asset.upper() if args.asset else None
        print(cumulative_report(Path(args.phase1810_cumulative), asset_filter))
        return

    _base.cli()

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("path", nargs="?", default="data/shadow_events.jsonl")
    parser.add_argument("--asset", default=None)
    parser.add_argument("--session", nargs="?", const="latest", default=None)
    args, _ = parser.parse_known_args(original_argv[1:])
    _, sidecar = _fast_session_rewrite(original_argv[1:])
    path = sidecar if sidecar is not None else Path(args.path)
    asset_filter = args.asset.upper() if args.asset else None

    print()
    print(_episode_table(path, asset_filter))


if __name__ == "__main__":
    cli()
