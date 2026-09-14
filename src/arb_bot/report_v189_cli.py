from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from statistics import median

from . import report_v187_cli as _base


ZERO = Decimal("0")


def _d(value) -> Decimal:
    return _base._d(value)


def _pct(values: list[Decimal], q: float) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    frac = Decimal(str(pos - lo))
    return ordered[lo] + (ordered[hi] - ordered[lo]) * frac


def _fmt(value: Decimal | None, digits: int = 2) -> str:
    return "-" if value is None else f"{float(value):.{digits}f}"


def _latency_table(path: Path, asset_filter: str | None) -> str:
    candidates = Counter()
    executions: dict[str, list[dict]] = defaultdict(list)
    targets: dict[str, int] = {}

    for row in _base._iter_rows(path) or ():
        event_type = row.get("event_type")
        p = row.get("payload", {})
        if asset_filter and str(p.get("asset") or "").upper() != asset_filter:
            continue
        strategy = str(p.get("strategy") or "")
        if event_type == "latency_candidate_v189":
            candidates[strategy] += 1
            targets[strategy] = int(p.get("target_latency_ms") or 0)
        elif event_type == "latency_execution_v189":
            executions[strategy].append(p)
            targets[strategy] = int(p.get("target_latency_ms") or 0)

    names = sorted(set(candidates) | set(executions), key=lambda name: targets.get(name, 10**9))
    lines = [
        "PHASE 1.8.9 BFOK LATENCY FRONTIER",
        "Only target arrival latency changes. Size=1, protected edge/coverage, 25 ms freshness, surge gate, fees, cooldown and recovery remain the same.",
        f"{'MODEL':11s} {'TGT':>4s} {'CAND':>6s} {'DONE':>6s} {'BOTH':>5s} {'MISS':>5s} {'NONE':>5s} {'WIN':>5s} {'LOSS':>5s} {'PNL':>10s} {'P(BOTH)':>8s} {'P50 ARR':>8s} {'P95 ARR':>8s} {'P50 SLIP':>9s} {'P95 SLIP':>9s} {'P50 ARR EDGE':>12s}",
        "-" * 145,
    ]
    if not names:
        lines.append("no protected latency-frontier candidates yet")
        return "\n".join(lines)

    for name in names:
        rows = executions.get(name, [])
        done = len(rows)
        both = sum(1 for p in rows if p.get("status") == "BOTH_FILLED")
        miss = sum(1 for p in rows if p.get("status") == "ONE_LEG_MISS")
        none = sum(1 for p in rows if p.get("status") == "NEITHER_FILLED")
        wins = sum(1 for p in rows if _d(p.get("realized_pnl")) > ZERO)
        losses = sum(1 for p in rows if _d(p.get("realized_pnl")) < ZERO)
        pnl = sum((_d(p.get("realized_pnl")) for p in rows), ZERO)
        arrivals = [_d(p.get("actual_arrival_ms")) for p in rows if p.get("actual_arrival_ms") is not None]
        slips = [_d(p.get("scheduler_slippage_ms")) for p in rows if p.get("scheduler_slippage_ms") is not None]
        arr_edges = [_d(p.get("arrival_market_edge_per_share")) for p in rows if p.get("arrival_market_edge_per_share") is not None]
        p_both = both / done * 100 if done else 0.0
        lines.append(
            f"{name:11s} {targets.get(name, 0):4d} {candidates.get(name, 0):6d} {done:6d} {both:5d} {miss:5d} {none:5d} "
            f"{wins:5d} {losses:5d} {float(pnl):+10.5f} {p_both:7.1f}% "
            f"{_fmt(_pct(arrivals, .50)):>8s} {_fmt(_pct(arrivals, .95)):>8s} "
            f"{_fmt(_pct(slips, .50)):>9s} {_fmt(_pct(slips, .95)):>9s} {_fmt(_pct(arr_edges, .50), 5):>12s}"
        )

    lines.extend(["", "ARRIVAL EDGE SURVIVAL"])
    for name in names:
        rows = executions.get(name, [])
        arr_edges = [_d(p.get("arrival_market_edge_per_share")) for p in rows if p.get("arrival_market_edge_per_share") is not None]
        if not arr_edges:
            continue
        positive = sum(1 for x in arr_edges if x > ZERO)
        floor = sum(1 for x in arr_edges if x >= Decimal("0.001"))
        lines.append(
            f"{name:11s} arrival_edge>0 {positive}/{len(arr_edges)} ({positive/len(arr_edges)*100:.1f}%) | "
            f">=+0.001 {floor}/{len(arr_edges)} ({floor/len(arr_edges)*100:.1f}%)"
        )
    return "\n".join(lines)


def _raw_observer_table(path: Path, asset_filter: str | None) -> str:
    rollups = Counter()
    positives: list[dict] = []
    for row in _base._iter_rows(path) or ():
        event_type = row.get("event_type")
        p = row.get("payload", {})
        if event_type == "raw_observer_rollup_v189" and not asset_filter:
            rollups.update(p.get("counts") or {})
        elif event_type == "raw_positive_observation_v189":
            if asset_filter and str(p.get("asset") or "").upper() != asset_filter:
                continue
            positives.append(p)

    lines = [
        "PHASE 1.8.9 RAW OBSERVER",
        "Observation-only probe. It does not submit or model an order lifecycle; positive P&L is a zero-latency local-book upper bound, not strategy equity.",
    ]
    if not positives and not rollups:
        lines.append("no RAW observer data yet")
        return "\n".join(lines)

    total_pnl = sum((_d(p.get("pnl_upper_bound")) for p in positives), ZERO)
    ages = [_d(p.get("older_book_age_ms")) for p in positives if p.get("older_book_age_ms") is not None]
    skews = [_d(p.get("book_age_skew_ms")) for p in positives if p.get("book_age_skew_ms") is not None]

    # Conservative episode count: repeated positive observations on the same
    # market within 500 ms are treated as one opportunity burst.
    episodes = 0
    grouped: dict[str, list[float]] = defaultdict(list)
    for p in positives:
        text = p.get("observed_at") or p.get("recorded_at")
        if text is None:
            # Current observer events do not need wall time for trading logic;
            # fall back to one observation per episode when absent.
            continue
        try:
            grouped[str(p.get("market_id") or "UNKNOWN")].append(
                datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
            )
        except (TypeError, ValueError):
            continue
    if grouped:
        for times in grouped.values():
            times.sort()
            last = None
            for ts in times:
                if last is None or ts - last > 0.5:
                    episodes += 1
                last = ts
    else:
        episodes = len(positives)

    if not asset_filter:
        lines.append(
            f"market_updates={rollups.get('MARKET_UPDATES', 0)} full_pairs={rollups.get('FULL_PAIR', 0)} "
            f"nonpositive={rollups.get('NONPOSITIVE', 0)}"
        )
    lines.append(
        f"positive_observations={len(positives)} independent_500ms_episodes={episodes} "
        f"positive_pnl_upper_bound={float(total_pnl):+.5f} "
        f"p50_older_book_age_ms={_fmt(_pct(ages, .50))} p50_age_skew_ms={_fmt(_pct(skews, .50))}"
    )

    buckets = [
        ("0-25ms", lambda x: x <= Decimal("25")),
        ("25-50ms", lambda x: Decimal("25") < x <= Decimal("50")),
        ("50-100ms", lambda x: Decimal("50") < x <= Decimal("100")),
        (">100ms", lambda x: x > Decimal("100")),
    ]
    lines.extend([f"{'OLDER BOOK AGE':15s} {'POS':>7s} {'UPPER PNL':>12s} {'P50 SKEW':>10s}", "-" * 52])
    for label, pred in buckets:
        selected = [p for p in positives if p.get("older_book_age_ms") is not None and pred(_d(p.get("older_book_age_ms")))]
        pnl = sum((_d(p.get("pnl_upper_bound")) for p in selected), ZERO)
        local_skew = [_d(p.get("book_age_skew_ms")) for p in selected if p.get("book_age_skew_ms") is not None]
        lines.append(f"{label:15s} {len(selected):7d} {float(pnl):+12.5f} {_fmt(_pct(local_skew, .50)):>10s}")
    return "\n".join(lines)


def cli() -> None:
    original_argv = list(sys.argv)
    _base.cli()

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("path", nargs="?", default="data/shadow_events.jsonl")
    parser.add_argument("--asset", default=None)
    parser.add_argument("--session", nargs="?", const="latest", default=None)
    args, _ = parser.parse_known_args(original_argv[1:])
    _, sidecar = _base._fast_session_rewrite(original_argv[1:])
    path = sidecar if sidecar is not None else Path(args.path)
    asset_filter = args.asset.upper() if args.asset else None

    print()
    print(_latency_table(path, asset_filter))
    print()
    print(_raw_observer_table(path, asset_filter))
    print()
    print(
        "Interpretation: LAT0 is a protected same-callback upper bound, not atomic live execution. "
        "Compare actual arrival, scheduler slip, edge survival and realized P&L across LAT0/1/2/3/5. "
        "Do not sum frontier variants."
    )


if __name__ == "__main__":
    cli()
