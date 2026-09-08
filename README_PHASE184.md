# Phase 1.8.4 — Fast Adverse-Selection Protection

Phase 1.8.4 is cumulative from `phase1.8.3/counterfactual-instrumentation` and turns the strongest Phase 1.8.3 findings into a stricter **simulation/shadow** execution policy.

It does **not** implement or enable live exchange order placement.

## Why this phase exists

Phase 1.8.3 showed that the main selective-maker failure occurs at the first fill:

- stale maker fills are heavily adversely selected;
- fee-adjusted maker-first/taker-second edge at fill is strongly associated with outcome quality;
- quote age and edge must be considered jointly;
- waiting for a second resting maker fill is rarely successful and often too slow;
- post-first-fill action selection cannot repair a first fill that was already toxic;
- ideal atomic opportunities exist, but most live for only a few milliseconds.

Phase 1.8.4 therefore focuses on avoiding bad first fills and measuring a more realistic fast complete-set execution frontier.

## 1. Fast pre-fill cancellation

Selective maker campaigns now continuously evaluate the economics of either resting leg filling first.

Default cancellation conditions:

- worst maker-first/taker-second edge `<= -0.005/share`;
- quote age `>= 500 ms` while worst edge is non-positive;
- hard quote age `>= 1,000 ms` regardless of edge;
- stale opposite book `>= 250 ms` while worst edge is non-positive.

A cancellation does not happen magically at the trigger. It enters a latency race.

Default simulated cancellation latency:

- `5 ms`

If the first maker fill occurs before the cancellation becomes effective, the fill wins the race and the real shadow campaign continues. If the cancellation deadline arrives first, the campaign is cancelled with zero fill P&L.

New events:

- `maker_variant_fast_cancel_intent_v184`
- `maker_variant_fast_cancel_effective_v184`
- `maker_variant_fast_cancel_race_lost_v184`

## 2. Timer-enforced quote-age guard

The edge sampler caches unchanged book states so the hot path does not repeatedly recompute identical quotes.

Quote age still advances even when no new market-data message arrives. Phase 1.8.4 therefore evaluates the 500 ms stale rule and the 1,000 ms hard quote-age rule independently on the normal strategy timer. A quiet websocket period cannot leave a stale selective maker quote resting indefinitely.

## 3. Pre-fill edge timeline

For every selective first fill, Phase 1.8.4 stores the most recent observable edge for the side that later filled first at approximately:

- 500 ms before fill;
- 250 ms before fill;
- 100 ms before fill;
- 50 ms before fill;
- 25 ms before fill;
- 10 ms before fill.

The samples include quote age, book ages and sampling-gap information.

New events:

- `maker_variant_prefill_timeline_v184`
- `maker_variant_prefill_timeline_outcome_v184`

The actual future first-fill side is used only for retrospective diagnosis. It is not an executable side-selection signal.

## 4. Faster post-fill research defaults

Phase 1.8.4 reduces the simulated delays that Phase 1.8.3 evidence showed were too slow:

- hybrid taker-completion latency: `5 ms`;
- hybrid missing-leg reprice interval: `25 ms`;
- hybrid inventory timeout: `500 ms`;
- maker inventory timeout: `750 ms`;
- strategy timer: `1 ms` fallback, while market updates remain event-driven.

These are research/shadow timings. They are not claims about achievable exchange-side latency.

## 5. Atomic execution proxy

The ideal atomic benchmark remains intact, but Phase 1.8.4 adds a stricter local-book execution proxy.

For every ideal capture it re-quotes the **full requested size** after independent end-to-end latency scenarios:

- `2 ms`;
- `5 ms`;
- `10 ms`.

At the execution deadline the proxy:

- requires both books to be fresh (default maximum age `25 ms`);
- re-checks full-size depth on both legs;
- includes protocol taker fees;
- includes the order-book slippage implied by the requested size;
- requires the opportunity still to meet the minimum execution edge.

Outcomes include:

- `EXECUTABLE_SHADOW_FILL`;
- `STALE_OR_MISSING_BOOK`;
- `NO_FULL_SIZE_QUOTE`;
- `NO_LONGER_PROFITABLE`;
- `EXPIRED_BEFORE_EXECUTION`.

New event:

- `atomic_execution_proxy_v184`

This remains a local-book shadow proxy. It does not prove simultaneous exchange fills, network acknowledgement, order matching priority or zero leg risk.

## 6. Report additions

`arb-report` keeps the existing Phase 1.8.3 report and appends:

- `PHASE 1.8.4 FAST PRE-FILL CANCELLATION`;
- `PHASE 1.8.4 PRE-FILL EDGE LOOKBACK`;
- `PHASE 1.8.4 ATOMIC EXECUTION PROXY`.

Use the existing session filter:

```bash
arb-report --session
```

The Phase 1.8.4 events also carry the Phase 1.8.3 process run id so the existing session filter remains compatible.

## Key environment overrides

```bash
V184_FAST_CANCEL_ENABLED=true
V184_PREFILL_CANCEL_EDGE_PER_SHARE=-0.005
V184_STALE_QUOTE_AGE_MS=500
V184_STALE_MAX_EDGE_PER_SHARE=0
V184_HARD_QUOTE_AGE_MS=1000
V184_CANCEL_LATENCY_MS=5
V184_MAX_OPPOSITE_BOOK_AGE_MS=250

V184_HYBRID_COMPLETION_LATENCY_MS=5
V184_HYBRID_MIN_REPRICE_INTERVAL_MS=25
V184_HYBRID_INVENTORY_TIMEOUT_MS=500
V184_MAKER_INVENTORY_TIMEOUT_MS=750
V184_STRATEGY_TIMER_INTERVAL_MS=1

V184_ATOMIC_EXECUTION_LATENCIES_MS=2,5,10
V184_ATOMIC_MIN_EXECUTION_EDGE_PER_SHARE=0.0001
V184_ATOMIC_MAX_BOOK_AGE_MS=25
```

## Running Phase 1.8.4

```bash
cd /workspaces/Arb-Bot
git fetch origin
git switch phase1.8.4/fast-adverse-selection
git pull
pip install -e '.[dev]'
pytest -q
arb-bot
```

In another terminal:

```bash
arb-report --session
```

Useful focused reports:

```bash
arb-report --session --strategy SHYB-97-I2-Q25
arb-report --session --asset BTC
arb-report --session --asset BNB
arb-report --session --asset DOGE
```

## Decision gate after data collection

Phase 1.8.4 should answer two practical questions:

1. Can a realistic cancellation latency prevent enough toxic first fills without cancelling too many genuine winners?
2. Does positive atomic P&L survive full-size depth, fees, book freshness and 2/5/10 ms end-to-end latency replay?

If maker cancellation remains unable to rescue expectancy while the atomic proxy remains positive, the next phase should prioritise the atomic/dual-order execution architecture rather than adding more maker parameter variants.
