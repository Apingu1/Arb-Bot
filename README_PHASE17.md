# Phase 1.7 — Multi-Asset Execution Frontier + ARB//TERM

Phase 1.7 expands the observer/shadow complete-set arbitrage research across BTC, ETH, HYPE, BNB, DOGE, XRP and SOL recurring 15-minute Up/Down markets.

The strategy remains non-directional. No fair-value prediction model, wallet, private key, signing, live order placement, geoblock bypass, VPN or proxy functionality is introduced.

## Research question

Phase 1.6 showed that apparently positive complete-set opportunities can be extremely short-lived. Phase 1.7 therefore separates three questions that should not be conflated:

1. **Does a profitable complete-set snapshot exist at all?** — measured by the ideal ATOMIC benchmark.
2. **Does that edge survive realistic non-atomic leg arrival?** — measured by the accelerated DFOK/RFOK frontier.
3. **Can passive two-sided acquisition work when both queues are unusually favorable?** — measured by the selective PMAKER controls.

## Ideal ATOMIC benchmark

`ATOMIC-BUY-S1/S5/S10/S20` and `ATOMIC-SELL-S1/S5/S10/S20` are benchmark-only controls.

They use the observed executable order-book depth and current Polymarket taker-fee formula, but assume both complementary legs can be captured simultaneously at one snapshot with zero inter-leg latency. ATOMIC therefore measures a theoretical execution ceiling, not a claim about available cross-order atomic execution.

ATOMIC captures never emit `strategy_equity` and are excluded from both Session Shadow P&L and All-Time Shadow P&L.

For each contiguous positive window the benchmark records:

- asset and market slug;
- size and direction;
- executable pair price;
- net edge after taker fees;
- benchmark P&L;
- opportunity lifetime and peak edge;
- edge-band counts at 0.1c, 0.2c, 0.3c, 0.5c and 1.0c per share.

Interpretation:

- ATOMIC materially positive + DFOK negative/zero -> execution latency/atomicity is the barrier.
- ATOMIC only marginally positive -> the underlying complete-set frontier itself is too thin.

## Accelerated DFOK/RFOK

The main small-size research profile is intentionally much faster than Phase 1.6:

```env
STRATEGY_TIMER_INTERVAL_MS=1
DUAL_FOK_BASE_LATENCY_MS=2
DUAL_FOK_PRIMARY_SIZE=1
DUAL_FOK_PRIMARY_EDGE_TARGET=0.001
DUAL_FOK_PRIMARY_STABILITY_MS=0
DUAL_FOK_SKEWS_MS=0,1,2,5,10,25
DUAL_FOK_EDGE_TARGETS=0.001,0.002,0.003,0.005,0.010
```

These are **shadow latency assumptions**, not claims that Codespaces or a production client can achieve those network round trips. The frontier exists to identify the latency at which observed edge disappears.

The 1 ms strategy timer is important: without it, a 2 ms DFOK assumption could still be serviced by the former 10 ms process loop.

DFOK opportunity-lifetime events now retain the market slug for per-asset analysis, and 10/20-share labels use plain `S10`/`S20` formatting.

## Selective PMAKER controls

Historical MAKER/HYBRID variants remain as controls. Phase 1.7 adds:

```text
PMAKER-Q25
PMAKER-Q50
PMAKER-Q100
PMAKER-Q250
```

PMAKER does not step deeper merely to create a target complete-set price. It joins the current best bids only when:

- the existing best-bid pair is already at or below the target pair;
- gross complete-set edge meets the minimum threshold;
- both displayed queues are below the variant cap;
- queue imbalance is within the configured maximum;
- the SURGE/toxic-flow and near-expiry safety conditions remain satisfied.

All maker expiry handling is generic across the seven recurring assets rather than BTC-specific.

## ARB//TERM

Running `arb-bot` serves the lightweight retro terminal dashboard on port 8765. `arb-ui` can serve the latest `data/dashboard_state.json` snapshot separately.

The primary balance is now **Session Shadow P&L**. Historical events populate a separate **All-Time Research P&L** so an old Phase 1.3–1.6 loss does not make a fresh Phase 1.7 process appear to be losing before it has traded.

The UI shows:

- Session Shadow P&L and All-Time Research P&L separately;
- ATOMIC benchmark rows marked `IDEAL` and excluded from shadow balances;
- live and next markets for every configured asset;
- per-model execution statistics for TAKER, MAKER, PMAKER, HYBRID, HEDGE, EV, DFOK and RFOK;
- an asset × model attribution table with session/all-time P&L and W/L;
- substantial events including wins, major losses, toxic ghost fills, fleeting arb windows and atomic captures;
- live message/order-book health.

## Default market universe

```env
MARKET_ASSETS=BTC,ETH,HYPE,BNB,DOGE,XRP,SOL
MARKET_LOOKAHEAD_INTERVALS=2
```

Recurring discovery is generic: `<asset>-updown-15m-<unix-window-start>`.

## Apply the complete Phase 1.7 research profile

```bash
bash scripts/apply_fast_arb_profile.sh
```

This updates the current `.env` with the accelerated taker/DFOK settings, 1 ms timer, ATOMIC benchmark and PMAKER controls without replacing unrelated environment settings.

Phase 1.7 remains observer/shadow-only throughout.
