# Arb-Bot — Phase 1.2 Multi-Strategy Shadow Research

Phase 1.2 is a **live-data research bot** for Polymarket BTC Up/Down 15-minute binary markets. It watches the real CLOB order books and compares three simulation-only strategies on the same market stream:

- **TAKER** — the original two-leg complete-set arb, with depth/fee checks, execution latency and delayed one-leg recovery.
- **MAKER** — passive bids on both outcomes, with zero maker trading fee in the shadow model and conservative trade-confirmed fill logic.
- **HYBRID** — maker-first inventory acquisition followed by a single taker completion leg when the remaining economics still clear the configured threshold.

**There is no wallet, private key, signing or live order-placement code.** The purpose is to establish which approach, if any, has positive expectancy after fees, latency, adverse selection and one-sided inventory risk.

## Live market handling

The recurring BTC series uses deterministic slugs:

```text
btc-updown-15m-{UTC_UNIX_INTERVAL_START}
```

The bot classifies each market as `LIVE`, `NEXT`, `FUTURE` or `EXPIRED`, subscribes only to LIVE + NEXT, and restricts strategy execution to the true current LIVE window. The NEXT book is kept warm so rollover is immediate.

For this recurring series, the slug timestamp is authoritative. Gamma's generic lifecycle flags can lag the exact 15-minute state, so they are not allowed to hide a valid current recurring market when token IDs are available.

## Shared market data

All three strategies use the exact same local books populated from Polymarket market WebSocket events:

- `book`
- `price_change`
- `last_trade_price`

The order books are depth-aware. Taker costs are calculated by walking the book level-by-level rather than assuming the best price is available for the full requested size.

## Strategy A — TAKER

For size `q`:

```text
expected_net(q)
  = q
  - executable_cost_A(q)
  - executable_cost_B(q)
  - taker_fees(q)
  - risk_reserve(q)
```

A LIVE opportunity is shadow-submitted only when it clears both the minimum expected profit and minimum net edge/share.

### Two-stage execution timing

The taker simulator now has two independent latency stages:

```text
opportunity detected
      ↓
SHADOW_LATENCY_MS
      ↓
try both FOK-style legs at detection-time marginal limits
      ↓
if exactly one fills
      ↓
SHADOW_RECOVERY_LATENCY_MS
      ↓
re-read the later book
      ↓
compare complete-missing-leg vs unwind-filled-leg
      ↓
choose the higher modeled P/L route
```

A dedicated ~10ms timer task drives these state transitions, so simulated execution/recovery no longer waits for the 10-second diagnostics heartbeat or depends on another WebSocket update arriving.

### Detailed taker records

Each finalized `taker_execution_summary` is self-contained and includes:

- detection timestamp;
- share size;
- detection best asks;
- detected cost and VWAP for both outcomes;
- detected marginal prices;
- expected fees, reserve and expected net profit;
- exact simulated first-stage fill prices/segments;
- configured and actual execution latency;
- whether A/B filled;
- delayed recovery latency;
- completion and unwind quotes;
- chosen recovery action;
- residual recovery penalty;
- realized P/L;
- equity after the attempt;
- empirical leg-miss statistics at that point.

## Empirical taker leg-risk

Phase 1.2 measures:

```text
estimated_leg_risk_per_share
  = P(one-leg miss)
  × average loss per share given a miss
```

The metric is reported live and saved with taker summaries. It is **measurement-only by default**:

```text
USE_EMPIRICAL_RISK_RESERVE=false
```

The original fixed `RISK_BUFFER_PER_SHARE` remains authoritative until enough observations exist to justify switching. `EMPIRICAL_RISK_MIN_SAMPLES` is retained as the future decision gate.

## Strategy B — MAKER

The pure maker simulator places virtual BUY orders at the observed best bid on both outcomes when:

```text
maker_bid_A + maker_bid_B
<= 1 - MAKER_MIN_GROSS_EDGE_PER_SHARE
```

Maker fees are modeled as zero.

### Conservative maker fill model

A maker order is **not** credited merely because a best bid disappears or the displayed book momentarily crosses.

A virtual maker BUY is only considered filled when, after the campaign was placed, the market stream reports a `last_trade_price` event that:

```text
side == SELL
trade_price <= our_bid
reported_trade_size >= our_simulated_order_size
```

This is still an approximation because real queue position is unknown, but it is intentionally stricter than simply assuming all best-bid changes fill us.

If both maker legs fill:

```text
realized P/L = shares × (1 - maker_fill_A - maker_fill_B)
```

If only one side fills, the engine holds the inventory for `MAKER_INVENTORY_TIMEOUT_MS`, then attempts to unwind into current bids and charges the taker fee on that unwind.

## Strategy C — HYBRID

The hybrid engine starts with the same conservative maker campaign.

If both maker orders fill, it behaves like the pure maker strategy.

If only one maker leg fills, it continuously evaluates a taker completion of the missing side:

```text
net = complete-set payout
      - maker acquisition cost
      - missing-side taker cost
      - missing-side taker fee
```

If this clears `HYBRID_MIN_NET_EDGE_PER_SHARE` and the absolute minimum-profit threshold, the completion is scheduled after `HYBRID_COMPLETION_LATENCY_MS` and must still fill within the detection-time marginal price limit.

If completion remains unattractive or misses, the engine keeps managing the one-sided inventory until `HYBRID_INVENTORY_TIMEOUT_MS`, then unwinds if necessary.

## Independent virtual equity curves

TAKER, MAKER and HYBRID each maintain independent realized equity, wins/losses, peak equity and max drawdown.

Every realized strategy event writes a `strategy_equity` row to the JSONL, allowing exact side-by-side comparison from the same market data.

Terminal diagnostics include a comparison line such as:

```text
STRATEGIES |
TAKER  eq=-5.3199 completed=0 misses=4 ... |
MAKER  eq=+... completed=... one_sided=... |
HYBRID eq=+... maker_only=... maker+taker=...
```

## Continuous edge research

`data/shadow_events.jsonl` also retains high-frequency `edge_observation` rows for LIVE/NEXT market states, even if no strategy executes.

This allows later analysis of:

- how often YES+NO fell below $1;
- the lowest taker pair observed;
- best fee-adjusted taker edge;
- maker bid-pair edge;
- executable size;
- timing within each 15-minute window;
- whether theoretical edges survived latency;
- leg-miss frequency and loss severity.

## Report command

Phase 1.2 adds:

```bash
arb-report
```

which reads `data/shadow_events.jsonl` by default and prints a compact strategy comparison.

To also generate a flat per-execution CSV:

```bash
arb-report --csv data/execution_summary.csv
```

The CSV includes strategy, status, action, shares, detected pair price, execution prices, execution latency, recovery latency, recovery prices, individual realized P/L and equity-after.

You can also point it at another saved run:

```bash
arb-report data/my_run.jsonl --csv data/my_run_summary.csv
```

## Install / run

Requires Python 3.12+.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
pytest
arb-bot
```

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
Copy-Item .env.example .env
pytest
arb-bot
```

## Updating from Phase 1 without losing the old run

Before starting a clean three-strategy comparison, preserve the existing Phase 1 dataset:

```bash
mv data/shadow_events.jsonl data/phase1_taker_only.jsonl
```

Then start Phase 1.2:

```bash
arb-bot
```

The old taker-only run remains available for comparison:

```bash
arb-report data/phase1_taker_only.jsonl
```

## Important settings

### Taker

- `MIN_NET_EDGE_PER_SHARE=0.005`
- `MIN_EXPECTED_PROFIT_USDC=0.10`
- `MIN_TRADE_SHARES=5`
- `MAX_TRADE_SHARES=100`
- `RISK_BUFFER_PER_SHARE=0.002`
- `RECOVERY_PENALTY_PER_SHARE=0.002`
- `SHADOW_LATENCY_MS=200`
- `SHADOW_RECOVERY_LATENCY_MS=100`
- `MARKET_COOLDOWN_MS=1000`
- `MAX_BOOK_AGE_MS=1500`

### Maker

- `MAKER_SHADOW_ENABLED=true`
- `MAKER_TRADE_SHARES=5`
- `MAKER_MIN_GROSS_EDGE_PER_SHARE=0.005`
- `MAKER_ORDER_TTL_MS=1500`
- `MAKER_INVENTORY_TIMEOUT_MS=2500`

### Hybrid

- `HYBRID_SHADOW_ENABLED=true`
- `HYBRID_TRADE_SHARES=5`
- `HYBRID_MIN_NET_EDGE_PER_SHARE=0.003`
- `HYBRID_COMPLETION_LATENCY_MS=100`
- `HYBRID_INVENTORY_TIMEOUT_MS=2500`

### Research / diagnostics

- `EMPIRICAL_RISK_MIN_SAMPLES=20`
- `USE_EMPIRICAL_RISK_RESERVE=false`
- `DIAGNOSTIC_INTERVAL_SECONDS=10`
- `EDGE_RECORD_MIN_INTERVAL_MS=0`
- `RUN_SECONDS=0`

## Safety / scope

This repository is research software, not a profitability guarantee. Phase 1.2 remains observer/shadow only and intentionally contains no wallet handling, signing, order submission or geoblock bypass. Any future live phase should only be enabled where the venue permits order placement and only after shadow evidence demonstrates robust positive expectancy under realistic execution assumptions.
