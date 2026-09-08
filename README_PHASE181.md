# Phase 1.8.1 — Selective Winner Strengthening

Phase 1.8.1 keeps the existing observer/shadow maker-family models and adds parallel out-of-sample variants derived from the Phase 1.7/1.8 event evidence.

No wallet, signing, live order placement, geoblock bypass, VPN, or proxy functionality is added. All executable-model P&L remains simulated pUSD. ATOMIC remains an ideal, non-executable benchmark.

## Selective variants

### Asymmetric Hybrid

Historical complete-set wins were enriched when the maker pair was at or below 0.97 and one displayed queue was materially smaller than the other.

- `SHYB-97-I2`
- `SHYB-97-I3`
- `SHYB-97-I4`
- `SHYB-97-I2-Q25`

`I2/I3/I4` are minimum queue-imbalance multiples. `Q25` additionally requires the smaller displayed queue to be no more than 25 shares.

### Tiny-queue paired maker

Historical PMAKER complete sets were enriched at deeper pairs with both queues small.

- `SPMAKER-P95-Q10`
- `SPMAKER-P97-Q10`
- `SPMAKER-P97-Q15`
- `SPMAKER-P97-Q25`

These join existing best bids only. They do not manufacture a deeper passive pair.

### Tiny-queue standard maker

- `SMAKER-97-Q10`

This preserves the original target-bid mechanism but only enters when both target-price queues are present, no larger than 10 shares, and reasonably balanced.

All selective variants use 1-share shadow size by default.

## Runtime controls

The new variants appear automatically in ARB//TERM's Model × Asset Control matrix. Selective variants default ON across the configured feed assets because their entry filters are already deliberately narrow. Any model or individual model×asset combination can be switched off at runtime.

Disabling a model stops new campaigns. Existing simulated inventory continues to be serviced until completion, cancellation, or unwind.

## First-fill telemetry

Every maker-family model in this phase is instrumented with `maker_variant_first_fill_snapshot` events. The final execution summary also carries the snapshot.

Recorded fields include:

- first fill side and time-to-first-fill
- placement queue sizes and imbalance
- whether the smaller queue filled first
- opposite best ask and displayed best-ask depth
- executable opposite quotes for 1 and 5 shares
- immediate complete-now net edge after taker fee
- 100/250/500 ms midpoint movement
- SURGE state and update rate
- seconds to expiry
- book ages

This is intended to identify the market state that separates complete-set success from toxic one-sided inventory.

## Correlated win episodes

Maker-family outcomes on the same market slug that finalize within 2 seconds (default) share a `market_episode_id`.

This prevents simultaneous wins across several counterfactual models from being interpreted as independent market discoveries.

Configure with:

```bash
V181_EPISODE_WINDOW_MS=2000
```

## Report

`arb-report` remains streaming and now reports:

- true complete-set wins
- independent winning episodes (`WIN_EP`)
- win rate
- average true-win P&L
- average losing-event P&L
- model × asset attribution
- selective first-fill win/loss diagnostics
- ideal ATOMIC benchmark separately

Examples:

```bash
arb-report
arb-report --asset SOL
arb-report --strategy SHYB-97-I2
arb-report --strategy SPMAKER-P97-Q10 --asset ETH
arb-report --all
```

## Main Phase 1.8.1 defaults

```text
V181_SELECTIVE_ENABLED=true
V181_SELECTIVE_TRADE_SHARES=1
V181_HYBRID_PAIR=0.97
V181_HYBRID_SMALL_QUEUE_CAP=25
V181_SELECTIVE_PAIR_MAX_IMBALANCE=2
V181_EPISODE_WINDOW_MS=2000
V181_MAKER_INVENTORY_TIMEOUT_MS=2500
```

The selective variants are experiments, not a claim of positive executable expectancy. The purpose of Phase 1.8.1 is to test whether the historically enriched entry conditions survive out-of-sample while keeping one-sided loss exposure small.
