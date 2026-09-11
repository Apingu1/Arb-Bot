from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path
from statistics import median

from . import report_v187_cli as _core
from . import report_v187_raw_cli as _raw


ZERO = Decimal("0")


def _median(values: list[Decimal]) -> Decimal | None:
    return median(values) if values else None


def _fmt(value: Decimal | None, *, digits: int = 2) -> str:
    if value is None:
        return "-"
    return f"{float(value):.{digits}f}"


def _freshness_table(path: Path, asset_filter: str | None) -> str:
    candidates = Counter()
    executions: dict[str, list[dict]] = defaultdict(list)
    limits: dict[str, int] = {}

    for row in _core._iter_rows(path) or ():
        event_type = row.get("event_type")
        p = row.get("payload", {})
        if asset_filter and str(p.get("asset") or "").upper() != asset_filter:
            continue
        strategy = str(p.get("strategy") or "")
        if event_type == "freshness_candidate_v188":
            candidates[strategy] += 1
            if p.get("max_book_age_ms") is not None:
                limits[strategy] = int(p["max_book_age_ms"])
        elif event_type == "freshness_execution_v188":
            executions[strategy].append(p)
            if p.get("max_book_age_ms") is not None:
                limits[strategy] = int(p["max_book_age_ms"])

    names = sorted(set(candidates) | set(executions), key=lambda n: limits.get(n, 10**9))
    lines = [
        "PHASE 1.8.8 BFOK FRESHNESS FRONTIER",
        "Only max accepted local-book age changes between variants. Edge, coverage, surge, cooldown, fee model, batch latency and recovery remain protected BFOK settings.",
        f"{'MODEL':14s} {'MAX':>5s} {'CAND':>6s} {'DONE':>6s} {'BOTH':>6s} {'MISS':>6s} {'WIN':>5s} {'LOSS':>5s} {'PNL':>11s} {'P(BOTH)':>8s} {'P50 DET OLD':>11s} {'P50 SKEW':>9s} {'P50 ARR OLD':>11s} {'P50 ARR EDGE':>12s}",
        "-" * 145,
    ]
    if not names:
        lines.append("no freshness-frontier candidates yet")
        return "\n".join(lines)

    for name in names:
        rows = executions.get(name, [])
        done = len(rows)
        both = sum(1 for p in rows if p.get("status") == "BOTH_FILLED")
        miss = sum(1 for p in rows if p.get("status") == "ONE_LEG_MISS")
        wins = sum(1 for p in rows if _core._d(p.get("realized_pnl")) > ZERO)
        losses = sum(1 for p in rows if _core._d(p.get("realized_pnl")) < ZERO)
        pnl = sum((_core._d(p.get("realized_pnl")) for p in rows), ZERO)
        p_both = both / done * 100 if done else 0.0
        det_old = [_core._d(p.get("detected_older_book_age_ms")) for p in rows if p.get("detected_older_book_age_ms") is not None]
        det_skew = [_core._d(p.get("detected_book_age_skew_ms")) for p in rows if p.get("detected_book_age_skew_ms") is not None]
        arr_old = [_core._d(p.get("arrival_older_book_age_ms")) for p in rows if p.get("arrival_older_book_age_ms") is not None]
        arr_edge = [_core._d(p.get("arrival_market_edge_per_share")) for p in rows if p.get("arrival_market_edge_per_share") is not None]
        lines.append(
            f"{name:14s} {limits.get(name, 0):5d} {candidates.get(name, 0):6d} {done:6d} {both:6d} {miss:6d} "
            f"{wins:5d} {losses:5d} {float(pnl):+11.5f} {p_both:7.1f}% "
            f"{_fmt(_median(det_old)):>11s} {_fmt(_median(det_skew)):>9s} {_fmt(_median(arr_old)):>11s} {_fmt(_median(arr_edge), digits=5):>12s}"
        )
    return "\n".join(lines)


def _raw_win_age_table(path: Path, asset_filter: str | None) -> str:
    rows = []
    for row in _core._iter_rows(path) or ():
        if row.get("event_type") != "raw_win_age_v188":
            continue
        p = row.get("payload", {})
        if asset_filter and str(p.get("asset") or "").upper() != asset_filter:
            continue
        rows.append(p)

    lines = [
        "PHASE 1.8.8 RAW WIN BOOK-AGE ATTRIBUTION",
        "Detailed ages for profitable zero-latency BFOK-RAW observations. This separates slightly-old books from obviously asynchronous/stale-book combinations.",
    ]
    if not rows:
        lines.append("no profitable BFOK-RAW observations with age telemetry yet")
        return "\n".join(lines)

    buckets = [
        ("0-25ms", lambda x: x <= Decimal("25")),
        ("25-35ms", lambda x: Decimal("25") < x <= Decimal("35")),
        ("35-50ms", lambda x: Decimal("35") < x <= Decimal("50")),
        ("50-75ms", lambda x: Decimal("50") < x <= Decimal("75")),
        ("75-100ms", lambda x: Decimal("75") < x <= Decimal("100")),
        (">100ms", lambda x: x > Decimal("100")),
    ]
    assigned = Counter()
    pnl_by_bucket: dict[str, Decimal] = defaultdict(lambda: ZERO)
    skew_by_bucket: dict[str, list[Decimal]] = defaultdict(list)
    unknown = 0
    for p in rows:
        raw_age = p.get("older_book_age_ms")
        if raw_age is None:
            unknown += 1
            continue
        age = _core._d(raw_age)
        label = next((name for name, predicate in buckets if predicate(age)), "UNKNOWN")
        assigned[label] += 1
        pnl_by_bucket[label] += _core._d(p.get("raw_pnl"))
        if p.get("book_age_skew_ms") is not None:
            skew_by_bucket[label].append(_core._d(p.get("book_age_skew_ms")))

    total_pnl = sum((_core._d(p.get("raw_pnl")) for p in rows), ZERO)
    older_values = [_core._d(p.get("older_book_age_ms")) for p in rows if p.get("older_book_age_ms") is not None]
    skew_values = [_core._d(p.get("book_age_skew_ms")) for p in rows if p.get("book_age_skew_ms") is not None]
    lines.append(
        f"raw_wins={len(rows)} raw_win_pnl_upper_bound={float(total_pnl):+.5f} "
        f"p50_older_book_age_ms={_fmt(_median(older_values))} p50_age_skew_ms={_fmt(_median(skew_values))} unknown_age={unknown}"
    )
    lines.extend([
        f"{'OLDER BOOK AGE':15s} {'WINS':>7s} {'RAW PNL':>12s} {'P50 SKEW':>10s}",
        "-" * 52,
    ])
    for label, _ in buckets:
        lines.append(
            f"{label:15s} {assigned.get(label, 0):7d} {float(pnl_by_bucket[label]):+12.5f} {_fmt(_median(skew_by_bucket[label])):>10s}"
        )
    return "\n".join(lines)


def cli() -> None:
    original_argv = list(sys.argv)
    _raw.cli()

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("path", nargs="?", default="data/shadow_events.jsonl")
    parser.add_argument("--asset", default=None)
    parser.add_argument("--session", nargs="?", const="latest", default=None)
    args, _ = parser.parse_known_args(original_argv[1:])
    _, sidecar = _core._fast_session_rewrite(original_argv[1:])
    path = sidecar if sidecar is not None else Path(args.path)
    asset_filter = args.asset.upper() if args.asset else None

    print()
    print(_freshness_table(path, asset_filter))
    print()
    print(_raw_win_age_table(path, asset_filter))
    print()
    print(
        "Interpretation: a profitable frontier at 35-50 ms with small age skew supports relaxing the 25 ms ceiling; "
        "profit concentrated at very old/high-skew books supports the stale-price-illusion hypothesis. Do not sum variant P&Ls."
    )


if __name__ == "__main__":
    cli()
