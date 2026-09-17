from __future__ import annotations

import json
from pathlib import Path

from arb_bot.report_v1810_cli import _episode_table
from arb_bot.report_v1810a import cumulative_report
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


def test_fast_cumulative_report_scans_archives_and_deduplicates_macro_surges(tmp_path):
    archive_dir = tmp_path / "sessions"
    archive_dir.mkdir()

    def episode(run_id, episode_id, second, pnl, asset="BNB"):
        observed = f"2026-09-15T00:00:{second:02d}Z"
        return [
            {
                "event_type": "raw_positive_observation_v189",
                "payload": {
                    "phase1810_run_id": run_id,
                    "recorded_at": observed,
                    "observed_at": observed,
                    "market_episode_id": episode_id,
                    "market_id": "bnb-market",
                    "asset": asset,
                    "fee_adjusted_edge_per_share": "0.02",
                    "surge_active": second >= 10,
                },
            },
            {
                "event_type": "batch_fok_execution_summary_v187",
                "payload": {
                    "phase1810_run_id": run_id,
                    "recorded_at": observed,
                    "finalized_at": observed,
                    "market_episode_id": episode_id,
                    "market_id": "bnb-market",
                    "asset": asset,
                    "strategy": "BFOK-10",
                    "status": "BOTH_FILLED",
                    "realized_pnl": pnl,
                },
            },
        ]

    first = episode("run-a", "a1", 0, "1") + episode("run-a", "a2", 15, "2")
    second = episode("run-b", "b1", 0, "4")
    (archive_dir / "phase1810_run-a.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in first) + "not-json\n"
    )
    (archive_dir / "phase1810_run-b.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in second)
    )

    report = cumulative_report(archive_dir)

    assert "files=2 lines=7 malformed=1 runs=2" in report
    assert "500ms         3        3         3" in report
    # The two run-a bursts merge at 30 seconds, while the run-b episode cannot
    # merge across the run boundary even though it has the same market ID/time.
    assert "30s           2        2         2" in report
    thirty_bfok = next(
        line for line in report.splitlines()
        if line.startswith("30s") and "BFOK-10" in line
    )
    assert "+5.00000" in thirty_bfok
    assert "+1.00000" in thirty_bfok
    assert "30-SECOND MACRO VIEW — ASSET × STRATEGY" in report
    assert "[FAIL] RAW sample: 3/50" in report
    assert "OVERALL: NOT READY" in report


def test_fast_cumulative_report_accepts_one_compact_file(tmp_path):
    compact = tmp_path / "compact.jsonl"
    compact.write_text("")
    report = cumulative_report(compact)
    assert "files=1 lines=0 malformed=0 runs=0" in report
    assert "archives=compact.jsonl" in report
    assert "[FAIL] BFOK-10 ex-best" in report
