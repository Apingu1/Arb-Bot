# Phase 1.8.5 — Profit-First PFOK

Phase 1.8.5 changes the research priority from broad maker experimentation to one canonical profit-seeking BUY complete-set shadow strategy named `PFOK`.

## What counts as profit

PFOK contributes to strategy equity only after a shadow order path has actually been attempted and finalized. Candidate detections and preflight rejects are no-trade decisions and never count as wins or zero-P&L trades.

A winning BOTH_FILLED event must:

1. Detect a positive complete-set BUY edge after taker fees.
2. Pass the configured depth/freshness gate.
3. Survive the configured end-to-end latency.
4. Pass a second full-size preflight quote using the original FOK limits.
5. Fill the first leg.
6. Wait at least the configured inter-leg gap before checking the second leg.
7. Fill the second leg while preserving the configured final minimum edge.

If the second leg disappears, PFOK retains the first-leg exposure, waits the configured recovery latency, chooses the better available completion/unwind action, and books the resulting P&L — including losses.

## Default profit-first filters

- sizes: 1, 2, 5 shares; largest qualifying size selected once per opportunity
- detection minimum edge: +0.015/share
- preflight minimum edge: +0.010/share
- final minimum edge: +0.005/share
- detection coverage: 3x requested size on both legs
- preflight coverage: 1.5x
- maximum book age: 10 ms
- base latency: 2 ms
- observed inter-leg gap: 1 ms
- recovery latency: 2 ms
- cooldown: 250 ms
- surge gate: enabled

These are deliberately selective defaults based on the Phase 1.8.4 observation that the only promising executable complete-set episodes retained roughly +0.026 to +0.036/share through the fast checkpoints, while the maker families were overwhelmingly negative at placement.

## Noise reduction

Phase 1.8.5 defaults the Phase 1.8.1 selective maker research OFF because it generated hundreds of thousands of rejection records without finalized trades. Historical maker models still exist in the runtime UI but start OFF. The mirrored SELL atomic control also starts OFF so BUY/SELL copies cannot inflate opportunity counts.

## Fresh Codespace / branch switch

```bash
cd /workspaces/Arb-Bot
git fetch origin
git switch phase1.8.5/profit-first-atomic
git pull origin phase1.8.5/profit-first-atomic
cp .env.example .env
python -m pip install -e '.[dev]'
pytest -q
arb-bot
```

Use a second terminal for the session report:

```bash
cd /workspaces/Arb-Bot
arb-report --session
```

The `PHASE 1.8.5 PROFIT-FIRST PFOK` section reports candidates, preflight rejects, submitted attempts, finalized trades, wins/losses, true realized PFOK shadow P&L, size attribution, asset attribution, execution timings and one-leg recovery outcomes.

## Scope

This remains shadow execution. It does not submit exchange orders or claim that local-book FOK fills prove real exchange fills. The purpose of this phase is to make the shadow model materially harder to fool while concentrating on the only path that has shown positive executable edge.
