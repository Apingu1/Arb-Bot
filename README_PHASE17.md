# Phase 1.7 — Multi-Asset ARB//TERM

Phase 1.7 expands the existing observer/shadow complete-set arbitrage research across BTC, ETH, HYPE, BNB, DOGE, XRP and SOL recurring 15-minute Up/Down markets.

The strategy remains non-directional. No fair-value prediction model, wallet, private key, signing, live order placement, geoblock bypass, VPN or proxy functionality is introduced.

## ARB//TERM

Running `arb-bot` now also serves a lightweight retro terminal dashboard on port 8765 by default. The dashboard is dependency-free and polls the bot's live in-process state. `arb-ui` can also serve the most recent `data/dashboard_state.json` snapshot separately.

The headline `SHADOW NET` is the sum of independent counterfactual shadow-model P&L. It is deliberately labelled as a research aggregate and must not be interpreted as one executable account balance.

The UI shows:

- live and next markets for each configured asset;
- Up/Down bid/ask, pair price, fee/risk-adjusted five-share edge and surge state;
- every TAKER, MAKER, HYBRID, HEDGE, EV, DFOK and RFOK model with P&L and key execution statistics;
- wins, major wins, losses, major losses, toxic ghost fills and fleeting DFOK opportunities;
- aggregate P&L by model family and asset;
- live message and order-book health.

## Configuration

```env
MARKET_ASSETS=BTC,ETH,HYPE,BNB,DOGE,XRP,SOL
MARKET_LOOKAHEAD_INTERVALS=2
DASHBOARD_ENABLED=true
DASHBOARD_HOST=0.0.0.0
DASHBOARD_PORT=8765
DASHBOARD_REFRESH_MS=500
DASHBOARD_STATE_PATH=data/dashboard_state.json
```

The recurring slug discovery is generic: `<asset>-updown-15m-<unix-window-start>`. Existing BTC discovery helpers remain for backwards compatibility.
