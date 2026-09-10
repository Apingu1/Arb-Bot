from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from . import report_v187_cli as _base


PROTECTED_MODELS = [
    "PFOK",
    "PFOK-S10",
    "PFOK-S20",
    "BFOK-1",
    "BFOK-5",
    "BFOK-10",
    "BFOK-20",
    "BFOK-EV",
]


def _funnel_table(path: Path, asset_filter: str | None) -> str:
    counts = Counter()
    model_entries = Counter()
    model_reasons: dict[str, Counter[str]] = defaultdict(Counter)
    for row in _base._iter_rows(path) or ():
        if row.get("event_type") != "opportunity_funnel_rollup_v187":
            continue
        p = row.get("payload", {})
        # Rollups are currently global; asset filtering remains available for
        # sparse RAW-win attribution below rather than fabricating per-asset
        # funnel counts from a global rollup.
        if asset_filter:
            continue
        counts.update(p.get("counts") or {})
        model_entries.update(p.get("model_entries") or {})
        for name, reasons in (p.get("model_reasons") or {}).items():
            model_reasons[name].update(reasons or {})

    lines = [
        "PHASE 1.8.7 OPPORTUNITY FUNNEL",
        "Compact counts show where observed market updates disappear before protected strategy entry. Edge counts use the ungated RAW-size pair after taker fees.",
    ]
    if asset_filter:
        lines.append("Per-asset funnel rollups are not emitted; use the RAW-WIN attribution table below for --asset filtering.")
        return "\n".join(lines)
    if not counts:
        lines.append("no opportunity-funnel rollups yet")
        return "\n".join(lines)

    ordered = [
        ("MARKET UPDATES", "MARKET_UPDATES"),
        ("LIVE MARKET UPDATES", "LIVE_MARKET_UPDATES"),
        ("TWO READY BOOKS", "TWO_READY_BOOKS"),
        ("TWO FRESH BOOKS", "TWO_FRESH_BOOKS"),
        ("FULL RAW-SIZE PAIR", "FULL_RAW_SIZE_PAIR"),
        ("EDGE >= -0.050", "EDGE_GE_M050"),
        ("EDGE >= -0.020", "EDGE_GE_M020"),
        ("EDGE >= 0", "EDGE_GE_0"),
        ("EDGE >= +0.001", "EDGE_GE_001"),
        ("EDGE >= +0.003", "EDGE_GE_003"),
        ("EDGE >= +0.005", "EDGE_GE_005"),
        ("COVERAGE >= 1.0x", "COVERAGE_GE_1X"),
        ("COVERAGE >= 1.5x", "COVERAGE_GE_1_5X"),
        ("SURGE ACTIVE", "SURGE_ACTIVE"),
        ("RAW ATTEMPTS", "RAW_ATTEMPTS"),
        ("RAW BOTH FILLED", "RAW_BOTH_FILLED"),
        ("RAW ONE-LEG MISS", "RAW_ONE_LEG_MISS"),
        ("RAW WINS", "RAW_WINS"),
        ("RAW LOSSES", "RAW_LOSSES"),
        ("RAW WINS BLOCKED BY ALL", "RAW_WINS_BLOCKED_BY_ALL_PROTECTED"),
    ]
    width = max(len(label) for label, _ in ordered)
    for label, key in ordered:
        lines.append(f"{label:<{width}s} {counts.get(key, 0):>10d}")

    lines.extend([
        "",
        "PROTECTED ENTRY FUNNEL",
        f"{'MODEL':12s} {'ENTERED':>9s}  DETECTION/BLOCK REASONS",
        "-" * 78,
    ])
    for name in PROTECTED_MODELS:
        reasons = model_reasons.get(name, Counter())
        reason_text = ", ".join(f"{k}:{v}" for k, v in reasons.most_common()) or "none"
        lines.append(f"{name:12s} {model_entries.get(name, 0):9d}  {reason_text}")
    return "\n".join(lines)


def _raw_win_attribution(path: Path, asset_filter: str | None) -> str:
    rows = []
    for row in _base._iter_rows(path) or ():
        if row.get("event_type") != "raw_win_attribution_v187":
            continue
        p = row.get("payload", {})
        if asset_filter and str(p.get("asset") or "").upper() != asset_filter:
            continue
        rows.append(p)

    lines = [
        "PHASE 1.8.7 RAW WIN -> PROTECTED MODEL ATTRIBUTION",
        "For every profitable zero-latency BFOK-RAW completion, this shows whether each protected model entered on the same update or which detection-stage gate blocked it. This is not a guarantee of later protected-model fill.",
    ]
    if not rows:
        lines.append("no profitable BFOK-RAW completions yet")
        return "\n".join(lines)

    raw_pnl = sum((_base._d(p.get("raw_pnl")) for p in rows), Decimal("0"))
    all_blocked = sum(1 for p in rows if p.get("all_protected_blocked"))

    # Collapse RAW wins into the same 500 ms market episode convention used by
    # the conservative PFOK/BFOK report, avoiding a misleading count of every
    # websocket update inside one persistent arbitrage burst.
    episodes = 0
    blocked_episodes = 0
    by_market = defaultdict(list)
    for p in rows:
        text = p.get("observed_at")
        try:
            ts = datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            continue
        by_market[str(p.get("market_id") or p.get("slug") or "UNKNOWN")].append((ts, p))
    for items in by_market.values():
        items.sort(key=lambda x: x[0])
        last = None
        episode_blocked = False
        for ts, p in items:
            if last is None or (ts - last) * 1000 > 500:
                if last is not None and episode_blocked:
                    blocked_episodes += 1
                episodes += 1
                episode_blocked = bool(p.get("all_protected_blocked"))
            else:
                episode_blocked = episode_blocked and bool(p.get("all_protected_blocked"))
            last = ts
        if last is not None and episode_blocked:
            blocked_episodes += 1

    lines.append(
        f"raw_win_observations={len(rows)} raw_win_pnl_upper_bound={float(raw_pnl):+.5f} "
        f"independent_500ms_episodes={episodes} all_protected_blocked={all_blocked} blocked_all_episodes={blocked_episodes}"
    )
    lines.extend([
        f"{'MODEL':12s} {'ENTERED':>8s} {'BLOCKED':>8s} {'ENTER%':>8s}  BLOCK REASONS ON RAW WINS",
        "-" * 96,
    ])

    for name in PROTECTED_MODELS:
        entered = 0
        blocked = 0
        reasons = Counter()
        for p in rows:
            info = (p.get("protected_models") or {}).get(name) or {}
            state = str(info.get("state") or "UNKNOWN")
            if state in {"SUBMITTED", "CANDIDATE"}:
                entered += 1
            else:
                blocked += 1
                reasons[str(info.get("reason") or "UNKNOWN")] += 1
        rate = entered / len(rows) * 100 if rows else 0.0
        reason_text = ", ".join(f"{k}:{v}" for k, v in reasons.most_common()) or "none"
        lines.append(f"{name:12s} {entered:8d} {blocked:8d} {rate:7.1f}%  {reason_text}")

    return "\n".join(lines)


def cli() -> None:
    # Reuse Phase 1.8.7 report machinery but include the deliberately ungated
    # BFOK-RAW diagnostic in BFOK, latency, economics and episode tables.
    if "BFOK-RAW" not in _base.BFOK_NAMES:
        _base.BFOK_NAMES.append("BFOK-RAW")
    if "BFOK-RAW" not in _base.CORE_NAMES:
        _base.CORE_NAMES.append("BFOK-RAW")

    original_argv = list(sys.argv)
    original_bfok_table = _base._bfok_table

    def bfok_table_with_raw_note(path, asset_filter):
        text = original_bfok_table(path, asset_filter)
        return (
            text
            + "\nBFOK-RAW: UNGATED ZERO-LATENCY UPPER-BOUND DIAGNOSTIC. "
              "It trades every structurally executable 1-share pair it observes, "
              "including negative-edge states. Do not treat RAW P&L as deployable live P&L."
        )

    _base._bfok_table = bfok_table_with_raw_note
    try:
        _base.cli()
    finally:
        _base._bfok_table = original_bfok_table

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("path", nargs="?", default="data/shadow_events.jsonl")
    parser.add_argument("--asset", default=None)
    parser.add_argument("--session", nargs="?", const="latest", default=None)
    args, _ = parser.parse_known_args(original_argv[1:])
    _, sidecar = _base._fast_session_rewrite(original_argv[1:])
    path = sidecar if sidecar is not None else Path(args.path)
    asset_filter = args.asset.upper() if args.asset else None

    print()
    print(_funnel_table(path, asset_filter))
    print()
    print(_raw_win_attribution(path, asset_filter))


if __name__ == "__main__":
    cli()
