from __future__ import annotations

import json
from pathlib import Path

from arb_bot.report_v183_cli import _family_edge_table, _ghost_tradeoff_table


def _write(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_family_edge_table_deduplicates_correlated_win_episodes(tmp_path):
    path = tmp_path / "family.jsonl"
    rows = []
    for strategy in ("SHYB-97-I2", "SHYB-97-I3"):
        rows.append(
            {
                "event_type": "maker_variant_execution_summary",
                "payload": {
                    "phase183_run_id": "run-1",
                    "strategy": strategy,
                    "slug": "doge-updown-15m-1",
                    "status": "MAKER_PLUS_TAKER_COMPLETED",
                    "realized_pnl": "0.01",
                    "market_episode_id": "doge-updown-15m-1:EP:1",
                    "first_fill_snapshot": {
                        "complete_now_net_edge_per_share": "-0.006"
                    },
                },
            }
        )
    _write(path, rows)

    text = _family_edge_table(
        path,
        run_id="run-1",
        strategy_filter=None,
        asset_filter=None,
    )
    assert "EDGE@FILL BY STRATEGY FAMILY / INDEPENDENT EPISODE" in text
    assert "SHYB" in text
    assert "-0.010..-0.005" in text
    # Two correlated model wins must collapse to one independent winning episode.
    shy = next(line for line in text.splitlines() if line.startswith("SHYB"))
    fields = shy.split()
    assert fields[2] == "2"  # N
    assert fields[3] == "1"  # EP
    assert fields[4] == "2"  # WINS
    assert fields[5] == "1"  # WIN_EP


def test_ghost_tradeoff_reports_losses_avoided_and_winners_killed(tmp_path):
    path = tmp_path / "ghost.jsonl"

    def event(pnl: str, status: str) -> dict:
        return {
            "event_type": "maker_variant_ghost_prefill_gate_result_v183",
            "payload": {
                "phase183_run_id": "run-1",
                "strategy": "SHYB-97-I2-Q25",
                "slug": "btc-updown-15m-1",
                "actual_pnl": pnl,
                "status": status,
                "results": {
                    "-0.010": {
                        "ANY_SIDE": {
                            "triggered": True,
                            "latencies": {
                                "25": {
                                    "would_avoid_first_fill": True,
                                    "counterfactual_pnl": "0",
                                    "delta_vs_actual": str(-float(pnl)),
                                }
                            },
                        }
                    }
                },
            },
        }

    _write(
        path,
        [
            event("-0.02", "PARTIAL_OR_ONE_SIDED_EXIT"),
            event("0.01", "MAKER_PLUS_TAKER_COMPLETED"),
        ],
    )

    text = _ghost_tradeoff_table(
        path,
        run_id="run-1",
        strategy_filter=None,
        asset_filter=None,
    )
    assert "GHOST ANY-SIDE CANCEL TRADE-OFF" in text
    row = next(line for line in text.splitlines() if line.strip().startswith("-0.010"))
    fields = row.split()
    assert fields[2] == "2"  # N
    assert fields[3] == "2"  # AVOID
    assert fields[4] == "0"  # RETAIN
    assert fields[5] == "1"  # LOSS_AV
    assert fields[6] == "1"  # POS_KILL
    assert fields[7] == "1"  # WIN_KILL
