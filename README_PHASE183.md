# Phase 1.8.3 — Counterfactual Decision Instrumentation

Phase 1.8.3 is a cumulative **observational** research phase based on `phase1.8.2/edge-timing-instrumentation`.

It does **not** change live/shadow execution decisions, entry thresholds, trade size, queue thresholds, inventory timeout, taker-completion threshold, unwind rules, runtime model×asset selections, or atomic benchmark status. The purpose is to answer the action-selection question exposed by Phase 1.8.2 before implementing a Phase 1.9 decision engine.

## Why this phase exists

Phase 1.8.2 showed that selective maker fills are still overwhelmingly one-sided and negative, but the losses are structured:

- fee-adjusted `EDGE@FILL` and quote age are much more informative than static queue imbalance;
- fills around or above roughly `-0.010/share` are materially better than the deeply negative population, but are not profitable enough to justify a hard gate yet;
- some deeply negative immediate-completion states later become complete-set wins through a second resting maker fill, so an immediate hedge/unwind rule can destroy genuine winners;
- the ideal atomic benchmark has rare 1–5 ms local-book survivors, but scheduler misses must be separated from opportunities that truly disappear before a checkpoint.

Phase 1.8.3 therefore measures the decision alternatives rather than enforcing one.

## 1. First-fill action counterfactual

For every newly instrumented selective first fill, the bot records three P&L alternatives:

- `COMPLETE_NOW` — immediately buy the missing complementary leg as taker;
- `UNWIND_NOW` — immediately sell the maker-filled exposure back into the book;
- `ACTUAL_POLICY` — allow the unchanged Phase 1.8.2 strategy to continue to its real shadow outcome.

At finalization the event stores:

- complete-now P&L;
- unwind-now P&L;
- actual eventual P&L/status/action;
- best of the three observed alternatives;
- actual-policy regret versus that best ex-post alternative.

New events:

- `maker_variant_first_fill_choices_v183`
- `maker_variant_first_fill_counterfactual_v183`

This is an ex-post research comparison. `best_action` is not an executable strategy signal by itself because it uses the eventual actual-policy outcome as one of the alternatives.

## 2. Second resting maker fill timing

The bot separately records whether the missing resting maker leg later fills after the first maker fill and how long that takes.

New event:

- `maker_variant_second_maker_fill_v183`

This distinguishes a profitable later **maker** completion from a taker rescue and lets the report quantify the value of waiting.

## 3. Ghost pre-fill cancellation study

While both maker orders are still resting, the bot continuously measures maker-first/taker-second completion economics for each possible first-fill side.

Candidate edge thresholds:

- `0`
- `-0.005`
- `-0.010`
- `-0.015`
- `-0.020`

For each threshold, cancellation races are replayed at:

- 5 ms
- 10 ms
- 25 ms
- 50 ms

Two scopes are retained:

- `ANY_SIDE` — realistic conservative policy: cancel the pair when either possible first-fill side becomes toxic;
- `ACTUAL_FIRST_SIDE` — oracle/research diagnostic using the side that eventually fills first. This is useful as an upper-bound diagnostic but is not directly executable because the future first side is not known at decision time.

If a ghost cancellation would have become effective before the actual first fill, the counterfactual P&L is zero for that campaign. **The real Phase 1.8.2 campaign is not cancelled.**

New events:

- `maker_variant_ghost_prefill_gate_trigger_v183`
- `maker_variant_ghost_prefill_gate_result_v183`

## 4. Quote age × EDGE@FILL

`arb-report` now reports joint buckets rather than treating fill age and fill edge independently.

Quote-age buckets:

- `<250ms`
- `250–500ms`
- `500–1000ms`
- `1–2.5s`
- `2.5–5s`
- `>=5s`

These are crossed against the existing `EDGE@FILL` bands and report sample count, true wins, win rate and average realized P&L.

## 5. Corrected instrumented-cohort trajectory denominator

The Phase 1.8.2 post-fill trajectory previously divided checkpoint samples by all historical selective outcomes, including Phase 1.8.1 outcomes that could never contain Phase 1.8.2 checkpoints.

Phase 1.8.3 replaces that table with an instrumented-cohort-only denominator and states the denominator explicitly.

## 6. Per-process session filtering

Each Phase 1.8.3 process gets a `phase183_run_id`.

Use:

```bash
arb-report --session
```

for the latest Phase 1.8.3 process, or:

```bash
arb-report --session <RUN_ID>
```

for a specific run.

This prevents newly collected evidence from being silently diluted by Phase 1.8.1/1.8.2 history while the ordinary unfiltered report still retains the full cumulative research record.

## 7. Atomic checkpoint outcome classification

Every ideal-atomic latency checkpoint is now classified as one of:

- `SURVIVED` — the local books were sampled at/after the target and the fee-adjusted complete-set edge remained positive;
- `EXPIRED_BEFORE_CHECKPOINT` — the observed ideal window closed before the target latency;
- `SCHEDULER_MISSED_CHECKPOINT` — the observed window lasted beyond the target latency, but the Python timer did not record a positive checkpoint sample.

For survivors the report shows both median remaining edge **and median remaining P&L**.

New events:

- `atomic_benchmark_capture_v183`
- `atomic_benchmark_lifetime_v183`
- `atomic_benchmark_latency_outcome_v183`

A scheduler miss does not prove the opportunity remained exchange-executable. It identifies that the current local Python scheduling loop did not establish a positive sample at that checkpoint despite the observed window duration reaching it.

## 8. Intentional legacy-control inactivity

Phase 1.8 defaults intentionally disable legacy TAKER, HEDGE, EV, DFOK and RFOK families. Their zero heartbeat activity must therefore **not** be interpreted as evidence that their strategy gates evaluated the market and found no opportunities.

The Phase 1.8.3 report prints this control note explicitly.

## Running Phase 1.8.3

```bash
cd /workspaces/Arb-Bot
git fetch origin
git switch phase1.8.3/counterfactual-instrumentation
git pull
pip install -e '.[dev]'
pytest -q
arb-bot
```

In another terminal:

```bash
arb-report
```

Focused examples:

```bash
arb-report --session
arb-report --session --strategy SHYB-97-I2-Q25
arb-report --session --asset BTC
arb-report --session --asset DOGE
arb-report --session --asset BNB
```

## Decision gate for Phase 1.9

Do not promote a hard `EDGE@FILL` threshold from Phase 1.8.3 alone without considering the counterfactual action table, second-maker-fill rate/timing, and realistic `ANY_SIDE` ghost cancellation results.

The Phase 1.9 decision should answer:

> Conditional on the pre-fill state, quote age and first-fill state, which executable action has the best expected value: avoid the fill, complete immediately, unwind immediately, or continue waiting for the second maker leg?

Until that evidence is strong enough, Phase 1.8.3 remains shadow/research only.
