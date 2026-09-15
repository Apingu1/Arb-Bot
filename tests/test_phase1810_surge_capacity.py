from __future__ import annotations

import json
from pathlib import Path

from arb_bot.report_v1810_cli import _episode_table
from arb_bot.storage_v1810 import LowLatencyJsonlRecorderV1810


def test_phase1810_recorder_shares_episode_id_and_archives(tmp_path, monkeypatch):
    monkeypatch.setenv("V186_CURRENT_SESSION_PATH", str(tmp_path / "current.jsonl"))
    monkeypatch.setenv("V1810_SESSION_ARCHIVE_DIR", str(tmp_path / "sessions"))
    recorder = LowLatencyJsonlRecorderV1810(str(tmp_path / "history.jsonl"))
    common = {"market_id": "market-1", "asset": "BNB"}
    recorder.write("raw_positive_observation_v189", {**common, "observed_at": "2026-09-15T00:00:00Z"})
    recorder.write(
        "batch_fok_execution_summary_v187",
        {**common, "strategy": "BFOK-10", "finalized_at": "2026-09-15T00:00:00.010Z", "realized_pnl": "1.2"},
    )
    archive = recorder.archive_path
    recorder.close()

    rows = [json.loads(line) for line in (tmp_path / "history.jsonl").read_text().splitlines()]
    ids = {row["payload"]["market_episode_id"] for row in rows}
    assert len(ids) == 1
    assert all(row["payload"]["phase_version"] == "1.8.10" for row in rows)
    assert archive.exists() and archive.read_text()


def test_phase1810_report_uses_one_trade_per_episode_and_excludes_best(tmp_path):
    path = Path(tmp_path / "events.jsonl")
    rows = []
    for episode, pnl, second in (("e1", "10", "5"), ("e2", "2", None)):
        rows.append({"event_type": "raw_positive_observation_v189", "payload": {
            "phase1810_run_id": "run1", "recorded_at": f"2026-09-15T00:00:0{len(rows)}Z",
            "observed_at": f"2026-09-15T00:00:0{len(rows)}Z", "market_episode_id": episode,
            "market_id": episode, "asset": "BNB", "fee_adjusted_edge_per_share": "0.02",
            "book_revision_a": 1, "book_revision_b": 1, "surge_active": episode == "e1",
        }})
        rows.append({"event_type": "batch_fok_execution_summary_v187", "payload": {
            "phase1810_run_id": "run1", "recorded_at": f"2026-09-15T00:00:0{len(rows)}Z",
            "finalized_at": f"2026-09-15T00:00:0{len(rows)}Z", "market_episode_id": episode,
            "market_id": episode, "asset": "BNB", "strategy": "BFOK-10", "realized_pnl": pnl,
        }})
        if second is not None:
            rows.append({"event_type": "batch_fok_execution_summary_v187", "payload": {
                "phase1810_run_id": "run1", "recorded_at": "2026-09-15T00:00:03Z",
                "finalized_at": "2026-09-15T00:00:03Z", "market_episode_id": episode,
                "market_id": episode, "asset": "BNB", "strategy": "BFOK-10", "realized_pnl": second,
            }})
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))

    report = _episode_table(path, None)
    bfok_line = next(line for line in report.splitlines() if line.startswith("BFOK-10"))
    assert "+17.00000" in bfok_line
    assert "+12.00000" in bfok_line
    assert "+2.00000" in bfok_line
    assert "MAJOR_SURGE" in report
