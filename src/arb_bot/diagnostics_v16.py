from __future__ import annotations

import logging
from decimal import Decimal


log = logging.getLogger(__name__)


def log_dual_fok_diagnostics(suite) -> None:
    """Phase 1.6 diagnostics kept separate to preserve older heartbeat code."""
    if suite is None:
        return
    for row in suite.diagnostic_rows():
        log.info(
            "%s | dir=%s eq=%+.4f pending=%d opp=%d life=%d avg/p50=%.0f/%.0fms | placed=%d both=%d miss=%d neither=%d p_both=%.1f%% p_miss=%.1f%% | S=%s E=%+.4f SK=%dms C=%sx ST=%dms base=%dms | edge=%+.4f cov=%.2fx miss_loss=%+.4f/sh EV/place=%+.5f | recovery=%d/%d/%d surge_block=%d",
            row["strategy"],
            row["direction"],
            float(row["equity"]),
            row["pending"],
            row["opportunities"],
            row["lifetime_samples"],
            float(row["avg_lifetime_ms"]),
            float(row["median_lifetime_ms"]),
            row["placements"],
            row["both_filled"],
            row["one_leg_miss"],
            row["neither_filled"],
            float(row["p_both"] * Decimal("100")),
            float(row["p_miss"] * Decimal("100")),
            row["shares"],
            float(row["edge_target"]),
            row["arrival_skew_ms"],
            row["coverage_multiple"],
            row["stability_ms"],
            row["base_latency_ms"],
            float(row["avg_detected_edge"]),
            float(row["avg_detected_coverage"]),
            float(row["avg_miss_loss_per_share"]),
            float(row["ev_per_placement"]),
            row["recovery_completions"],
            row["recovery_unwinds"],
            row["recovery_liquidity_failures"],
            row["surge_blocks"],
        )

    ranked = suite.ranked_rows()
    if ranked:
        log.info(
            "DUAL-FOK TOP | %s",
            " | ".join(
                f"{row['strategy']} EV={float(row['ev_per_placement']):+.5f} both={float(row['p_both'] * Decimal('100')):.1f}% miss={float(row['p_miss'] * Decimal('100')):.1f}%"
                for row in ranked[:5]
            ),
        )
