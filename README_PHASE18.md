# Phase 1.8 — Winner Strengthening

Phase 1.8 follows the Phase 1.7 multi-asset experiment and narrows active executable-shadow research to models that produced true profitable complete-set completions in the supplied logs.

## Active executable-shadow models

- `HYBRID-99`
- `HYBRID-98`
- `PMAKER-Q100`
- `PMAKER-Q250`

A **true win** means a profitable completed complete set (`MAKER_PLUS_TAKER_COMPLETED`, `BOTH_MAKER_FILLED`, or equivalent). A profitable `PARTIAL_OR_ONE_SIDED_EXIT` is not counted as an arbitrage win.

The supplied Phase 1.7 evidence shows the true wins on ETH, so `V18_WINNER_ASSETS=ETH` is the default evidence gate. This can be widened later when another asset demonstrates repeatable true completions.

## Strengthened defaults

Hybrid:
- 1 share instead of 5
- 25ms completion latency benchmark instead of 100ms
- 2.5s inventory timeout
- 0.010/share maximum hold loss
- empirical risk gate after 3 filled campaigns
- only 0.99 and 0.98 targets

Paired maker:
- only Q100 and Q250 variants
- 1 share
- minimum gross edge 0.010/share
- maximum queue imbalance 2x
- empirical risk gate after 3 filled campaigns

## Inactive by default

The following remain in the repository for historical comparison but are not active Phase 1.8 executable-shadow experiments:

- legacy TAKER
- MAKER-99/98/97/96
- PMAKER-Q25/Q50
- HEDGE
- EV frontier
- DFOK
- RFOK
- SPLITSELL

ATOMIC remains enabled because it is a non-executable benchmark ceiling and is excluded from shadow P&L.

## Compact report

`arb-report` now streams the JSONL file instead of loading the entire run into memory. Default output shows only active winner models plus any historical model with a true complete-set win.

```bash
arb-report
arb-report --asset ETH
arb-report --all
```

This keeps large research ledgers reviewable and avoids terminal/output termination caused by the previous full historical dump.
