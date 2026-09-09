from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import arb_bot.profit_fok_v185 as pfok_mod
import arb_bot.profit_fok_v185_diag as diag_mod
from arb_bot.config_v185 import SettingsV185
from arb_bot.models import MarketPair
from arb_bot.profit_fok_v185_diag import DiagnosedProfitFOKSuiteV185, NamedDiagnosedProfitFOKEngineV185
from arb_bot.report_v185_cli import _episode_table
from arb_bot.storage import JsonlRecorder
from arb_bot.strategy import ArbitrageEngine


def _pair() -> MarketPair:
    return MarketPair(
        market_id="m-live",
        condition_id="c-live",
        slug="eth-updown-15m-1999999800",
        question="ETH Up or Down 15m",
        outcome_a="Up",
        outcome_b="Down",
        token_a="A",
        token_b="B",
    )


def _engine(settings) -> ArbitrageEngine:
    engine = ArbitrageEngine(settings)
    engine.set_markets([_pair()])
    engine.books["A"].apply_snapshot(
        [{"price": "0.38", "size": "100"}],
        [{"price": "0.40", "size": "100"}],
    )
    engine.books["B"].apply_snapshot(
        [{"price": "0.48", "size": "100"}],
        [{"price": "0.50", "size": "100"}],
    )
    return engine


def _settings(**kwargs) -> SettingsV185:
    values = dict(
        v185_profit_sizes=(Decimal("1"), Decimal("2"), Decimal("5")),
        v185_detection_min_edge_per_share=Decimal("0.005"),
        v185_preflight_min_edge_per_share=Decimal("0.003"),
        v185_final_min_edge_per_share=Decimal("0.001"),
        v185_detection_coverage_multiple=Decimal("1.5"),
        v185_preflight_coverage_multiple=Decimal("1.0"),
        v185_max_book_age_ms=1000,
        v185_base_latency_ms=0,
        v185_leg_gap_ms=0,
        v185_recovery_latency_ms=0,
        v185_cooldown_ms=0,
        v185_use_surge_gate=False,
        v185_parallel_pfok_enabled=True,
        v185_gate_sample_interval_ms=100000,
        v185_frontier_gate_sample_interval_ms=100000,
    )
    values.update(kwargs)
    return SettingsV185(**values)


def test_frontier_keeps_control_unchanged_and_adds_six_variants(tmp_path):
    settings = _settings()
    suite = DiagnosedProfitFOKSuiteV185(settings, JsonlRecorder(str(tmp_path / "events.jsonl")))

    names = [variant.strategy for variant in suite.variants]
    assert names == [
        "PFOK",
        "PFOK-EDGE3",
        "PFOK-DEPTH1",
        "PFOK-NOSURGE",
        "PFOK-AGGR",
        "PFOK-S10",
        "PFOK-S20",
    ]

    control = suite.variants[0]
    assert control.settings is settings
    assert control.settings.v185_profit_sizes == (Decimal("1"), Decimal("2"), Decimal("5"))
    assert control.settings.v185_detection_min_edge_per_share == Decimal("0.005")
    assert control.settings.v185_preflight_min_edge_per_share == Decimal("0.003")
    assert control.settings.v185_final_min_edge_per_share == Decimal("0.001")
    assert control.settings.v185_detection_coverage_multiple == Decimal("1.5")
    assert control.settings.v185_preflight_coverage_multiple == Decimal("1.0")

    by_name = {variant.strategy: variant for variant in suite.variants}
    assert by_name["PFOK-EDGE3"].settings.v185_detection_min_edge_per_share == Decimal("0.003")
    assert by_name["PFOK-DEPTH1"].settings.v185_detection_coverage_multiple == Decimal("1.0")
    assert by_name["PFOK-NOSURGE"].settings.v185_use_surge_gate is False
    assert by_name["PFOK-AGGR"].settings.v185_final_min_edge_per_share == Decimal("0.0005")
    assert by_name["PFOK-S10"].settings.v185_profit_sizes == (Decimal("10"),)
    assert by_name["PFOK-S20"].settings.v185_profit_sizes == (Decimal("20"),)


def test_named_variant_books_equity_under_its_own_strategy(tmp_path, monkeypatch):
    monkeypatch.setattr(pfok_mod, "market_phase", lambda pair: pfok_mod.MarketPhase.LIVE)
    monkeypatch.setattr(diag_mod, "market_phase", lambda pair: diag_mod.MarketPhase.LIVE)
    settings = _settings(v185_parallel_pfok_enabled=False)
    path = tmp_path / "named.jsonl"
    strategy = NamedDiagnosedProfitFOKEngineV185(
        settings,
        JsonlRecorder(str(path)),
        "PFOK-EDGE3",
    )
    engine = _engine(settings)

    strategy.on_market_update(engine, "m-live")
    strategy.process_due(engine)
    pending = strategy.pending["m-live"]
    pending.second_due = time.monotonic() - 1
    strategy.process_due(engine)

    assert strategy.equity.wins == 1
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    summary = next(row["payload"] for row in rows if row["event_type"] == "dual_fok_execution_summary")
    equity = next(row["payload"] for row in rows if row["event_type"] == "strategy_equity")
    assert summary["strategy"] == "PFOK-EDGE3"
    assert equity["strategy"] == "PFOK-EDGE3"


def test_report_deduplicates_correlated_pfok_variants_into_one_episode(tmp_path):
    path = tmp_path / "episodes.jsonl"
    t0 = datetime(2026, 9, 9, 7, 0, 0, tzinfo=timezone.utc)
    rows = []
    for offset_ms, strategy in ((0, "PFOK"), (20, "PFOK-EDGE3"), (40, "PFOK-NOSURGE")):
        rows.append(
            {
                "event_type": "dual_fok_execution_summary",
                "payload": {
                    "phase183_run_id": "r1",
                    "strategy": strategy,
                    "mode": "PROFIT_FOK_V185",
                    "market_id": "m1",
                    "slug": "eth-updown-15m-1",
                    "finalized_at": (t0 + timedelta(milliseconds=offset_ms)).isoformat().replace("+00:00", "Z"),
                    "realized_pnl": "0.10",
                },
            }
        )
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")

    output = _episode_table(path, run_id="r1", asset_filter=None)
    assert "independent_episodes=1" in output
    assert "control_participated=1" in output
    assert "episodes_with_any_win=1" in output
