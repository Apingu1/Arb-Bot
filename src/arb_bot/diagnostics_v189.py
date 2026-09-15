from __future__ import annotations

import logging
from decimal import Decimal


log = logging.getLogger(__name__)
ZERO = Decimal("0")


def _number(row: dict, key: str, default=0):
    value = row.get(key, default)
    return default if value is None else value


def log_latency_isolation_diagnostics_v189(suite) -> None:
    """Log heterogeneous Phase 1.8.9 rows without assuming PFOK fields.

    Phase 1.8.9 combines PFOK, BFOK, the latency frontier and a RAW observer in
    one dashboard suite.  Their diagnostic dictionaries deliberately differ,
    so the legacy Phase 1.6 formatter cannot safely index every row.
    """
    if suite is None:
        return

    for row in suite.diagnostic_rows():
        strategy = str(row.get("strategy") or "UNKNOWN")
        mode = str(row.get("mode") or "UNKNOWN")
        direction = str(row.get("direction") or "UNKNOWN")
        target = row.get("target_latency_ms")
        target_text = "-" if target is None else f"{target}ms"
        log.info(
            "%s | mode=%s dir=%s eq=%+.4f pending=%d candidates=%d opp=%d "
            "placed=%d completed=%d both=%d miss=%d neither=%d "
            "p_both=%.1f%% p_miss=%.1f%% target=%s avg_arrival=%.2fms "
            "EV/place=%+.5f",
            strategy,
            mode,
            direction,
            float(_number(row, "equity", ZERO)),
            int(_number(row, "pending")),
            int(_number(row, "candidates", _number(row, "opportunities"))),
            int(_number(row, "opportunities", _number(row, "candidates"))),
            int(_number(row, "placements")),
            int(_number(row, "completed")),
            int(_number(row, "both_filled")),
            int(_number(row, "one_leg_miss", _number(row, "misses"))),
            int(_number(row, "neither_filled")),
            float(_number(row, "p_both", ZERO) * Decimal("100")),
            float(_number(row, "p_miss", ZERO) * Decimal("100")),
            target_text,
            float(_number(row, "avg_arrival_ms", ZERO)),
            float(_number(row, "ev_per_placement", ZERO)),
        )

    ranked = suite.ranked_rows()
    if ranked:
        log.info(
            "PHASE 1.8.9 TOP | %s",
            " | ".join(
                f"{row.get('strategy', 'UNKNOWN')} EV={float(_number(row, 'ev_per_placement', ZERO)):+.5f}"
                for row in ranked[:5]
            ),
        )
