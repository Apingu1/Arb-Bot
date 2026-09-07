# Arb-Bot — Phase 1 Shadow Arbitrage Engine

Phase 1 is a **live-data research bot** for Polymarket BTC Up/Down 15-minute binary markets. It watches the real CLOB order books, calculates fee- and depth-adjusted complete-set arbitrage, and simulates dual-leg execution after configurable latency.

**It does not contain live order-placement code.** This is deliberate. The first objective is to establish whether the strategy has positive net expectancy after fees, depth, stale quotes, latency and one-leg misses.

## What Phase 1 does

- Discovers the recurring BTC 15-minute series using deterministic `btc-updown-15m-{unix_start}` event slugs, with Gamma public search retained as a fallback.
- Subscribes to both outcome-token books on the official market WebSocket.
- Maintains local full-depth books from snapshots and incremental price changes.
- Prices every candidate size using actual executable depth, not best ask alone.
- Applies the current crypto taker-fee model to every price level.
- Adds a configurable per-share execution-risk reserve.
- Selects the size with the highest expected net profit within limits.
- Simulates latency before attempting the two legs.
- Models four execution outcomes: both filled, neither filled, A-only, B-only.
- On a one-leg miss, compares completing the missing leg against unwinding the filled leg and chooses the less damaging modeled recovery.
- Records opportunities and shadow results to JSONL for later statistical analysis.
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

A trade is only shadow-submitted when both the minimum expected profit and minimum net edge per share are satisfied.

## Why this is safer than `YES + NO < 1`

A displayed pair below $1 can still be a losing trade after taker fees, depth/slippage and execution risk. Phase 1 therefore walks the entire ask book and computes fees per fill level. It never assumes the top-of-book price is available for the full desired size.

## BTC 15-minute discovery

The recurring BTC series uses event slugs of the form:

```text
btc-updown-15m-{UTC_UNIX_INTERVAL_START}
```

The bot derives the current and nearby 15-minute slugs and fetches those events directly from Gamma. This avoids depending on public-search ranking.

For this recurring series, expiry is derived from the slug itself:

```text
interval_start = unix timestamp encoded in slug
interval_end   = interval_start + 900 seconds
```

This prevents calendar-day/event-level end-date values from incorrectly expiring a live 15-minute window.

If zero markets are produced, discovery now logs a rejection summary and one compact market sample, e.g.:

```text
Discovery rejection summary: {'closed': 1, 'inactive': 8, 'missing_tokens': 1}
Discovery sample market fields: {...}
```

That makes future Gamma/schema changes immediately diagnosable.

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

Results are written by default to `data/shadow_events.jsonl`.

## Important settings

- `MIN_NET_EDGE_PER_SHARE=0.005` — require at least 0.5c expected net edge/share after modeled costs.
- `MIN_EXPECTED_PROFIT_USDC=0.10` — ignore tiny absolute opportunities.
- `MAX_TRADE_SHARES=100` — hard Phase 1 sizing ceiling.
- `RISK_BUFFER_PER_SHARE=0.002` — reserve 0.2c/share beyond explicit fees and book depth.
- `SHADOW_LATENCY_MS=200` — delay between detection and simulated FOK execution.
- `MARKET_COOLDOWN_MS=1000` — prevents repeatedly counting the same gap every book update.
- `MAX_BOOK_AGE_MS=1500` — rejects evaluation if either side is stale.
- `RUN_SECONDS=0` — zero means run continuously; set e.g. `600` for a ten-minute capture.

## Output events

The JSONL file contains `opportunity` records and `shadow_result` records. After a meaningful sample, evaluate opportunity frequency, both-fill rate, leg-miss rate, average win, average/max miss loss, cumulative P&L, drawdown, size sensitivity and latency sensitivity.

## Tests

The suite covers:

- midpoint crypto fee math;
- rejection of a 97c midpoint pair after taker fees;
- acceptance of a sufficiently wide fee-adjusted pair;
- full-depth sizing;
- recurring BTC 15-minute slug alignment;
- exact interval derivation from recurring slugs;
- Gamma event-to-market parsing;
- protection against day-level expiry values;
- rejection of genuinely finished recurring windows.

## Safety / scope

This repository is currently research software, not a profitability guarantee. Phase 1 intentionally has no wallet key handling, signing, order submission or geoblock bypass. Any future live phase should only be enabled where the venue permits order placement and only after shadow data demonstrates sufficiently robust positive expectancy.
