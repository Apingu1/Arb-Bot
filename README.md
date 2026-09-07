# Arb-Bot — Phase 1 Live-Window Shadow Arbitrage Engine

Phase 1 is a **live-data research bot** for Polymarket BTC Up/Down 15-minute binary markets. It watches the real CLOB order books, records pair economics even when no trade qualifies, calculates fee- and depth-adjusted complete-set arbitrage, and simulates dual-leg execution after configurable latency.

**It does not contain live order-placement code.** The first objective is to establish whether the strategy has positive net expectancy after fees, depth, stale quotes, latency and one-leg misses.

## What Phase 1 does

- Discovers the recurring BTC 15-minute series using deterministic `btc-updown-15m-{unix_start}` event slugs, with Gamma public search retained as a fallback.
- Classifies every recurring market as `LIVE`, `NEXT`, `FUTURE` or `EXPIRED` from the slug's Unix start time.
- Subscribes only to `LIVE` and immediately `NEXT`, keeping the next book warm for rollover while excluding distant pre-open books.
- Permits shadow-arbitrage intents **only on the true LIVE window**.
- Maintains local full-depth books from snapshots and incremental price changes.
- Records high-frequency `edge_observation` rows for LIVE/NEXT even when the pair is not profitable.
- Prices candidate sizes using actual executable depth, not best ask alone.
- Applies the current crypto taker-fee model to every fill level.
- Adds a configurable per-share execution-risk reserve.
- Selects the size with the highest expected net profit within limits.
- Simulates latency before attempting the two legs.
- Models both filled, neither filled and one-leg-miss outcomes.
- On a one-leg miss, compares completing the missing leg against unwinding the filled leg and chooses the less damaging modeled recovery.
- Tracks best observed pair price, raw edge and fee/risk-adjusted net edge for each 15-minute window.
- Checks Polymarket's geoblock endpoint at startup for visibility, while remaining observer-only regardless of location.

## Core decision rule

For size `q`, Phase 1 evaluates:

```text
expected_net(q)
  = q
  - executable_cost_outcome_A(q)
  - executable_cost_outcome_B(q)
  - taker_fees(q)
  - risk_reserve(q)
```

A LIVE-window trade is only shadow-submitted when both the minimum expected profit and minimum net edge per share are satisfied.

## Why this is safer than `YES + NO < 1`

A displayed pair below $1 can still be a losing trade after taker fees, depth/slippage and execution risk. Phase 1 therefore walks the ask books and computes fees per fill level. It never assumes the top-of-book price is available for the full desired size.

## LIVE / NEXT / FUTURE handling

The recurring BTC series uses:

```text
btc-updown-15m-{UTC_UNIX_INTERVAL_START}
```

For example, if the current UTC window is 06:15–06:30:

```text
06:15 slug -> LIVE
06:30 slug -> NEXT
06:45+     -> FUTURE
<06:15     -> EXPIRED
```

The stream subscribes to LIVE + NEXT only. At 06:30 the already-warm NEXT market becomes LIVE immediately, even before the next Gamma refresh.

For this deterministic recurring series, the slug clock is authoritative. Gamma's generic `active`, `closed`, `enableOrderBook` and day-level expiry fields can lag or describe a broader event state, so they are not allowed to hide an otherwise valid current recurring window when token IDs are available.

## Continuous edge research

`data/shadow_events.jsonl` now contains three important event types:

- `edge_observation` — every relevant LIVE/NEXT book state by default;
- `opportunity` — a LIVE state that passes the fee/risk/depth thresholds;
- `shadow_result` — simulated result after execution latency.

Each edge observation includes best bid/ask on both outcomes, top pair price, raw edge, top ask depth, minimum-size executable pair price, taker fees, risk reserve, net edge/profit, book age, market phase and seconds to window start/end.

This means a run can answer not only “how many trades fired?” but also:

- how often YES+NO fell below $1;
- the lowest pair price observed;
- whether the gap remained positive after fees;
- how much executable size existed;
- when in the 15-minute window gaps appeared;
- how frequently a theoretical edge survived simulated execution latency.

## Diagnostics

The terminal heartbeat now concentrates on LIVE/NEXT and reports best-observed window statistics, e.g.:

```text
LIVE ... [06:15-06:30Z] | Up 0.43/0.45 ask_depth=317 | Down 0.54/0.56 ask_depth=202 |
top_pair=1.01 raw=-0.0100 | 5sh VWAP=1.01 fees=... risk=... net=... |
best_pair=0.972 best_raw=+0.0280 best_net=-0.0060 obs=1842
```

Distant future books no longer dominate the terminal or the shadow decision path.

## Install

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

## Docker

```bash
cp .env.example .env
docker compose up --build
```

## Important settings

- `MIN_NET_EDGE_PER_SHARE=0.005` — require at least 0.5c expected net edge/share after modeled costs.
- `MIN_EXPECTED_PROFIT_USDC=0.10` — ignore tiny absolute opportunities.
- `MAX_TRADE_SHARES=100` — hard Phase 1 sizing ceiling.
- `RISK_BUFFER_PER_SHARE=0.002` — reserve 0.2c/share beyond explicit fees and book depth.
- `SHADOW_LATENCY_MS=200` — delay between detection and simulated FOK execution.
- `MARKET_COOLDOWN_MS=1000` — prevents repeatedly counting the same qualifying gap every update.
- `MAX_BOOK_AGE_MS=1500` — rejects shadow evaluation if either side is stale.
- `DIAGNOSTIC_INTERVAL_SECONDS=10` — terminal heartbeat frequency.
- `EDGE_RECORD_MIN_INTERVAL_MS=0` — zero records every relevant update; increase this only if JSONL volume becomes excessive.
- `RUN_SECONDS=0` — zero means run continuously; set e.g. `3600` for a one-hour capture.

## Tests

The suite covers fee math, fee-adjusted rejection, full-depth sizing, recurring slug alignment, exact 15-minute windows, LIVE/NEXT/FUTURE/EXPIRED classification, LIVE+NEXT stream selection, lagging Gamma status flags, date-level expiry handling, finished-window rejection and edge-observation statistics.

## Safety / scope

This repository is research software, not a profitability guarantee. Phase 1 intentionally has no wallet key handling, signing, order submission or geoblock bypass. Any future live phase should only be enabled where the venue permits order placement and only after shadow data demonstrates sufficiently robust positive expectancy.
