from __future__ import annotations

import json
from decimal import Decimal

import arb_bot.profit_fok_v185 as pfok185
import arb_bot.profit_fok_v186 as pfok186
from arb_bot.config_v186 import SettingsV186
from arb_bot.discovery import MarketPhase
from arb_bot.latency_runtime_v186 import LatencyFirstDualFOKSuiteV186, LatencyFirstMakerResearchSuiteV186, PreciseFastPFOKSuiteV186
from arb_bot.models import MarketPair
from arb_bot.profit_fok_v186 import FastPFOKSuiteV186
from arb_bot.storage import JsonlRecorder
from arb_bot.storage_v186 import LowLatencyJsonlRecorderV186
from arb_bot.strategy_v186 import ArbitrageEngineV186


def _pair() -> MarketPair:
    return MarketPair(
        market_id="m186",
        condition_id="c186",
        slug="btc-updown-15m-1999999800",
        question="BTC Up or Down 15m",
        outcome_a="Up",
        outcome_b="Down",
        token_a="A",
        token_b="B",
    )


def _settings(**kwargs) -> SettingsV186:
    defaults = dict(
        v181_selective_enabled=False,
        v185_profit_fok_enabled=True,
        v185_parallel_pfok_enabled=True,
        v185_profit_sizes=(Decimal("1"), Decimal("2"), Decimal("5")),
        v185_detection_min_edge_per_share=Decimal("0.005"),
        v185_preflight_min_edge_per_share=Decimal("0.003"),
        v185_final_min_edge_per_share=Decimal("0.001"),
        v185_detection_coverage_multiple=Decimal("1.5"),
        v185_preflight_coverage_multiple=Decimal("1.0"),
        v185_max_book_age_ms=1000,
        v185_leg_gap_ms=1,
        v185_recovery_latency_ms=0,
        v185_cooldown_ms=0,
        v185_use_surge_gate=False,
        v186_fast_pfok_enabled=True,
        v186_fast_snapshot_sizes=(Decimal("1"), Decimal("2"), Decimal("5"), Decimal("10"), Decimal("20")),
        v186_fast1_latency_ms=0,
        v186_fast2_latency_ms=0,
        v186_fast_size_latency_ms=0,
    )
    defaults.update(kwargs)
    return SettingsV186(**defaults)


def _engine(settings: SettingsV186) -> ArbitrageEngineV186:
    engine = ArbitrageEngineV186(settings)
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


def test_v186_keeps_existing_control_frontier_and_adds_six_fast_models(tmp_path):
    settings = _settings()
    recorder = JsonlRecorder(str(tmp_path / "suite.jsonl"))
    research = LatencyFirstMakerResearchSuiteV186(settings, recorder)
    dual = LatencyFirstDualFOKSuiteV186(settings, recorder)

    names = [row["strategy"] for row in dual.diagnostic_rows()]
    assert "PFOK" in names
    assert "PFOK-S10" in names
    assert "PFOK-S20" in names
    assert "PFOK-NOSURGE" in names
    assert "PFOK-DEPTH1" in names
    assert "PFOK-AGGR" in names
    assert "PFOK-EDGE3" in names
    assert {"PFOK-FAST1", "PFOK-FAST2", "PFOK-REQUOTE1", "PFOK-REQUOTE2", "PFOK-FAST-S10", "PFOK-FAST-S20"}.issubset(names)
    assert dual.fast is research.fast
    assert isinstance(research.fast, PreciseFastPFOKSuiteV186)


def test_v186_requote_can_survive_offsetting_leg_price_move(tmp_path, monkeypatch):
    monkeypatch.setattr(pfok185, "market_phase", lambda pair: MarketPhase.LIVE)
    monkeypatch.setattr(pfok186, "market_phase", lambda pair: MarketPhase.LIVE)

    settings = _settings()
    recorder = JsonlRecorder(str(tmp_path / "requote.jsonl"))
    suite = FastPFOKSuiteV186(settings, recorder)
    engine = _engine(settings)

    suite.on_market_update(engine, "m186", surge=None)

    # Same combined pair economics, but A worsens by one cent while B improves
    # by one cent. Old per-leg price locks reject A>0.40; re-quote variants can
    # accept the current 0.41+0.49 complete set if fees/edge still pass.
    engine.books["A"].apply_snapshot(
        [{"price": "0.39", "size": "100"}],
        [{"price": "0.41", "size": "100"}],
    )
    engine.books["B"].apply_snapshot(
        [{"price": "0.47", "size": "100"}],
        [{"price": "0.49", "size": "100"}],
    )
    suite.process_due(engine)

    by_name = {variant.strategy: variant for variant in suite.variants}
    assert by_name["PFOK-FAST1"].preflight_rejects == 1
    assert by_name["PFOK-REQUOTE1"].placements == 1
    assert by_name["PFOK-REQUOTE1"].pending["m186"].preflight_edge is not None
    assert by_name["PFOK-REQUOTE1"].pending["m186"].preflight_edge > 0


def test_v186_recorder_rolls_up_gate_samples_and_writes_session_sidecar(tmp_path, monkeypatch):
    main_path = tmp_path / "shadow.jsonl"
    session_path = tmp_path / "current.jsonl"
    monkeypatch.setenv("V186_CURRENT_SESSION_PATH", str(session_path))
    monkeypatch.setenv("V186_GATE_ROLLUP_SECONDS", "1")

    recorder = LowLatencyJsonlRecorderV186(str(main_path))
    recorder.write(
        "profit_fok_gate_sample_v185",
        {
            "phase183_run_id": "abc",
            "phase184_run_id": "def",
            "phase185_run_id": "ghi",
            "strategy": "PFOK",
            "mode": "PROFIT_FOK_V185",
            "gate_reason": "EDGE_BELOW_DETECTION",
            "sample_edge_per_share": Decimal("-0.01"),
            "sample_coverage": Decimal("2"),
            "book_age_a_ms": Decimal("1"),
            "book_age_b_ms": Decimal("2"),
        },
    )
    recorder.flush()
    recorder.close()

    rows = [json.loads(line) for line in main_path.read_text().splitlines()]
    assert not any(row["event_type"] == "profit_fok_gate_sample_v185" for row in rows)
    rollup = next(row for row in rows if row["event_type"] == "profit_fok_gate_rollup_v186")
    assert rollup["payload"]["samples"] == 1
    assert rollup["payload"]["gate_reasons"]["EDGE_BELOW_DETECTION"] == 1
    assert session_path.exists()
    assert "profit_fok_gate_rollup_v186" in session_path.read_text()
