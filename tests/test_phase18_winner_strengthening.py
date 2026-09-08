from __future__ import annotations

import json
from pathlib import Path

from arb_bot.config_v18 import SettingsV18
from arb_bot.report_v18 import build_compact
from arb_bot.runtime_controls_v18 import RuntimeControlsV18
from arb_bot.storage import JsonlRecorder
from arb_bot.winner_research_v18 import WinnerResearchSuiteV18


def test_v18_ignores_stale_phase17_flags(monkeypatch):
    monkeypatch.setenv("HEDGE_SHADOW_ENABLED", "true")
    monkeypatch.setenv("EV_FRONTIER_ENABLED", "true")
    monkeypatch.setenv("DUAL_FOK_ENABLED", "true")
    monkeypatch.setenv("HYBRID_TRADE_SHARES", "5")

    settings = SettingsV18()
    assert settings.maker_enabled is True  # engine available; runtime matrix gates it
    assert settings.hedge_enabled is False
    assert settings.ev_frontier_enabled is False
    assert settings.dual_fok_enabled is False
    assert str(settings.hybrid_trade_shares) == "1"


def test_v18_instantiates_runtime_controllable_maker_family(tmp_path: Path):
    settings = SettingsV18()
    recorder = JsonlRecorder(str(tmp_path / "events.jsonl"))
    suite = WinnerResearchSuiteV18(settings, recorder)
    names = {variant.strategy_name for variant in suite.variants}
    assert names == {
        "MAKER-99", "MAKER-98", "MAKER-97", "MAKER-96",
        "HYBRID-99", "HYBRID-98", "HYBRID-97", "HYBRID-96",
        "PMAKER-Q25", "PMAKER-Q50", "PMAKER-Q100", "PMAKER-Q250",
    }


def test_runtime_controls_are_model_asset_specific():
    controls = RuntimeControlsV18()
    controls.configure(["HYBRID-99", "PMAKER-Q100"])
    assert controls.enabled_for("HYBRID-99", "BTC")
    assert controls.enabled_for("HYBRID-99", "ETH")
    assert not controls.enabled_for("HYBRID-99", "SOL")
    controls.set_asset("HYBRID-99", "SOL", True)
    assert controls.enabled_for("HYBRID-99", "SOL")
    controls.set_asset("HYBRID-99", "BTC", False)
    assert not controls.enabled_for("HYBRID-99", "BTC")
    controls.set_model("PMAKER-Q100", False)
    assert not controls.model_enabled("PMAKER-Q100")


def test_compact_report_counts_only_true_complete_set_wins(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    rows = [
        {
            "event_type": "maker_variant_execution_summary",
            "payload": {
                "strategy": "HYBRID-99",
                "slug": "eth-updown-15m-1",
                "status": "MAKER_PLUS_TAKER_COMPLETED",
                "realized_pnl": "0.0626",
            },
        },
        {
            "event_type": "maker_variant_execution_summary",
            "payload": {
                "strategy": "PMAKER-Q100",
                "slug": "btc-updown-15m-1",
                "status": "PARTIAL_OR_ONE_SIDED_EXIT",
                "realized_pnl": "0.0056",
            },
        },
        {
            "event_type": "maker_variant_execution_summary",
            "payload": {
                "strategy": "PMAKER-Q100",
                "slug": "eth-updown-15m-1",
                "status": "BOTH_MAKER_FILLED",
                "realized_pnl": "0.0100",
            },
        },
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    result = build_compact(path)
    assert result["stats"]["HYBRID-99"]["true_wins"] == 1
    assert result["stats"]["PMAKER-Q100"]["true_wins"] == 1
    assert result["stats"]["PMAKER-Q100"]["events"] == 2
    assert result["stats"]["PMAKER-Q100"]["assets"]["BTC"]["true_wins"] == 0
