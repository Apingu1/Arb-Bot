import json
from decimal import Decimal

from arb_bot.report import build_report


def test_report_surfaces_zero_execution_ev_activity_and_ghosts(tmp_path):
    path = tmp_path / "phase15.jsonl"
    rows = [
        {
            "event_type": "hedge_campaign_placed",
            "payload": {"strategy": "EV-G250-S5-E05-L100", "slug": "s1"},
        },
        {
            "event_type": "hedge_campaign_cancelled",
            "payload": {
                "strategy": "EV-G250-S5-E05-L100",
                "slug": "s1",
                "reason": "HEDGEABILITY_GRACE_EXPIRED",
            },
        },
        {
            "event_type": "hedge_ghost_outcome",
            "payload": {
                "strategy": "EV-G250-S5-E05-L100",
                "slug": "s1",
                "outcome": "WOULD_FILL",
                "would_be_profitable": True,
                "would_clear_original_target": False,
            },
        },
        {
            "event_type": "split_sell_execution_summary",
            "payload": {
                "strategy": "SPLITSELL-05",
                "slug": "s1",
                "status": "SPLIT_SELL_COMPLETE",
                "action": "BOTH_PASSIVE_SELLS_FILLED",
                "shares": "5",
                "target_edge_per_share": "0.005",
                "sell_price_a": "0.50",
                "sell_price_b": "0.51",
                "realized_pnl": "0.05",
                "equity_after": "0.05",
            },
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    summary, executions = build_report(path)

    ev = summary["EV-G250-S5-E05-L100"]
    assert ev["events"] == 0
    assert ev["placements"] == 1
    assert ev["cancellations"] == 1
    assert ev["ghost_fills"] == 1
    assert ev["ghost_profitable"] == 1

    split = summary["SPLITSELL-05"]
    assert split["events"] == 1
    assert split["pnl"] == Decimal("0.05")
    assert any(row["strategy"] == "SPLITSELL-05" for row in executions)
