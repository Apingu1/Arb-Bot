# Phase 1.8.2 — Edge / Fill Timing Instrumentation

Phase 1.8.2 is an **observational research phase** layered on top of the accepted Phase 1.8.1 selective-strengthening branch.

It deliberately does **not** change order placement rules, selective thresholds, taker-completion rules, inventory timeouts, unwind logic, trade size, or model×asset defaults. The purpose is to collect better evidence before changing strategy behaviour.

## Selective maker telemetry

For SHYB / SPMAKER / SMAKER variants the runtime now records:

- quoted maker gross edge at placement;
- time from quote placement to first fill;
- fee-adjusted hedgeable complete-set edge at first fill;
- deterioration from quoted maker gross edge to hedgeable edge at first fill;
- first-fill-to-finalization time;
- total campaign age at finalization;
- post-fill hedgeable edge checkpoints at approximately 25, 50, 100, 250 and 500 ms while residual inventory remains.

New event types:

- `maker_variant_first_fill_timing_v182`
- `maker_variant_post_fill_edge_sample`

Execution summaries are enriched with `first_fill_to_finalize_ms`, `campaign_age_ms`, and `post_fill_edge_timeline` when available.

## Ideal atomic timing telemetry

The ideal atomic benchmark remains explicitly non-executable and excluded from shadow P&L.

Each ideal capture now tracks whether the same fee-adjusted complete-set opportunity is still observed at/after approximate latency checkpoints:

- 1 ms
- 2 ms
- 5 ms
- 10 ms
- 25 ms
- 50 ms
- 100 ms

Every sample stores both the requested checkpoint and the **actual observed elapsed time**. This avoids pretending a delayed book update occurred at an exact latency.

New event type:

- `atomic_benchmark_latency_sample`

Heartbeat atomic diagnostics now include survival counts such as `1ms:5/7` alongside the existing average/median opportunity lifetime.

## `arb-report` additions

`arb-report` keeps the existing Phase 1.8.1 tables and adds:

1. **SELECTIVE EDGE@FILL BUCKETS** — win rate, average P&L and fill timing split into edge-at-fill bands.
2. **SELECTIVE POST-FILL EDGE TRAJECTORY** — observed hedgeable edge at the post-fill checkpoints.
3. **SELECTIVE OUTCOME QUALITY** — separates complete-set wins, profitable non-complete outcomes, negative outcomes and flats.
4. **ATOMIC IDEAL-FILL TIMING** — p50/p90/max lifetime plus observed survival percentages at 1/2/5/10/25/50 ms. Existing historical lifetime events are included.
5. **ATOMIC LATENCY REPLAY** — Phase 1.8.2-only observed checkpoint survival and median remaining edge.

BUY and SELL atomic rows remain mirrored benchmark views and must not be added together as independent opportunities.

## Recommended run

After switching to this branch and reinstalling the editable package if required:

```bash
pip install -e '.[dev]'
arb-bot
```

In another terminal:

```bash
arb-report
```

Useful focused views:

```bash
arb-report --strategy SHYB-97-I2-Q25
arb-report --asset DOGE
arb-report --asset BNB
```

The next strategy decision should be based on the new edge-at-fill buckets and post-fill trajectory, not on adding another arbitrary queue threshold.
