# Phase 1.8.10 — Surge Concentration & Real Capacity Validation

Phase 1.8.10 keeps the Phase 1.8.9 shadow execution behaviour unchanged. It
adds run-aware attribution so a burst containing many repeated detections is
not mistaken for many independent opportunities.

## What is new

- One shared `market_episode_id` across PFOK, BFOK, latency and RAW events.
- A compact append-only evidence file for every run under
  `data/phase1810_sessions/`. Only attribution events and periodic RAW rollups
  are duplicated, keeping unrelated high-volume writes off the latency path.
- Surge measurements and local book revisions on positive RAW observations.
- Conservative first-execution-per-strategy-per-episode P&L.
- P&L after removing the best episode and top-one/top-three concentration.
- Normal, moderate and major-surge episode attribution.
- A dual-book-refresh capacity proxy that requires newer revisions on both
  outcome books before repeated displayed liquidity is counted again.

The refresh proxy remains a shadow diagnostic. It cannot guarantee that a live
order would receive the displayed liquidity.

## Run

After switching to the branch, reinstall the editable package so the console
commands point to the Phase 1.8.10 entrypoints:

```bash
pip install -e '.[dev]'
arb-bot
```

Generate a report for the current process:

```bash
arb-report --session --all | tee data/phase1_8_10_report.txt
```

Generate the fast cumulative report across all compact Phase 1.8.10 run
archives:

```bash
arb-report --phase1810-cumulative | tee data/phase1_8_10a_cumulative_report.txt
```

This mode does not rescan the multi-million-line historical event file. It
reads each `data/phase1810_sessions/phase1810_*.jsonl` archive once and then
calculates all report sections in memory. A compact file can also be supplied
explicitly:

```bash
arb-report --phase1810-cumulative data/phase1_8_10_compact_cumulative.jsonl
```

The report compares 500 ms, 5 second, 30 second and 60 second quiet-gap
definitions. Runs are always kept separate. It reports one-trade-per-episode
P&L, P&L excluding the best episode, concentration, asset/regime breakdowns,
BFOK-10 miss losses and PASS/FAIL validation gates.

## Decision rule

Do not progress to live-readiness work until the cumulative evidence contains:

- at least 50 independent RAW-positive episodes;
- at least 25 realistic BFOK-10 episodes;
- positive BFOK-10 P&L after its single best episode is removed;
- acceptable one-leg miss losses; and
- evidence across multiple time windows rather than one isolated surge.

For a deterministic report gate, BFOK-10 first-trade one-leg miss losses may
consume no more than 25% of its gross winning P&L. This reporting threshold
does not change execution or entry settings.
