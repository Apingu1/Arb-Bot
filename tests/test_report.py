import json
from decimal import Decimal

from arb_bot.report import build_report, write_csv


def test_report_aggregates_three_strategy_summaries_and_writes_csv(tmp_path):
    path = tmp_path / "shadow.jsonl"
    rows = [
        {
            "event_type": "taker_execution_summary",
            "payload": {
                "strategy": "TAKER",
                "slug": "s1",
                "status": "ONE_LEG_MISS",
                "action": "UNWIND_FILLED_LEG",
                "shares": "5",
                "detected_pair_vwap": "0.94",
                "actual_execution_latency_ms": "205",
                "realized_pnl": "-1.25",
                "equity_after": "-1.25",
            },
        },
        {
            "event_type": "maker_execution_summary",
            "payload": {
                "strategy": "MAKER",
                "slug": "s1",
                "status": "BOTH_MAKER_FILLED",
                "action": "MERGE_COMPLETE_SET",
                "shares": "5",
                "maker_bid_a": "0.48",
                "maker_bid_b": "0.50",
                "realized_pnl": "0.10",
                "equity_after": "0.10",
            },
        },
        {
            "event_type": "hybrid_execution_summary",
            "payload": {
                "strategy": "HYBRID",
                "slug": "s1",
                "status": "MAKER_PLUS_TAKER_COMPLETED",
                "action": "MERGE_COMPLETE_SET",
                "shares": "5",
                "maker_bid_a": "0.40",
                "maker_bid_b": "0.50",
                "realized_pnl": "0.16",
                "equity_after": "0.16",
            },
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    summary, executions = build_report(path)

    assert summary["TAKER"]["pnl"] == Decimal("-1.25")
    assert summary["MAKER"]["pnl"] == Decimal("0.10")
    assert summary["HYBRID"]["pnl"] == Decimal("0.16")
    assert len(executions) == 3

    csv_path = tmp_path / "summary.csv"
    write_csv(executions, csv_path)
    text = csv_path.read_text(encoding="utf-8")
    assert "ONE_LEG_MISS" in text
    assert "BOTH_MAKER_FILLED" in text
    assert "MAKER_PLUS_TAKER_COMPLETED" in text
