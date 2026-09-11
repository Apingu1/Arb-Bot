from __future__ import annotations

import json
import time
from decimal import Decimal
from types import SimpleNamespace

import arb_bot.batch_fok_raw_v187 as raw187
import arb_bot.batch_fok_v187 as bfok187
from arb_bot.batch_fok_raw_v187 import BatchFOKWithRawSuiteV187
from arb_bot.config_v188 import SettingsV188
from arb_bot.discovery import MarketPhase
from arb_bot.freshness_frontier_v188 import FreshnessFrontierSuiteV188
from arb_bot.models import MarketPair
from arb_bot.runtime_v188 import BatchFirstMakerResearchSuiteV188
from arb_bot.storage import JsonlRecorder
from arb_bot.storage_v186 import LowLatencyJsonlRecorderV186
from arb_bot.strategy_v186 import ArbitrageEngineV186


def _pair() -> MarketPair:
    return MarketPair(
        market_id="fresh188",
        condition_id="cfresh188",
        slug="sol-updown-15m-1999999800",
        question="SOL Up or Down 15m",
        outcome_a="Up",
        outcome_b="Down",
        token_a="A",
        token_b="B",
    )


def _settings(**kwargs) -> SettingsV188:
    defaults = dict(
        v181_selective_enabled=False,
        v187_batch_fok_enabled=True,
        v187_batch_sizes=(Decimal("1"),),
        v187_detection_min_edge_per_share=Decimal("0.005"),
        v187_detection_coverage_multiple=Decimal("1.5"),
        v187_max_book_age_ms=25,
        v187_batch_arrival_latency_ms=1,
        v187_recovery_latency_ms=1,
        v187_cooldown_ms=250,
        v187_use_surge_gate=True,
        v187_ev_enabled=False,
        v187_raw_enabled=True,
        v187_raw_size=Decimal("1"),
        v188_freshness_frontier_enabled=True,
        v188_freshness_ages_ms=(25, 35, 50, 75, 100),
        v188_freshness_size=Decimal("1"),
    )
    defaults.update(kwargs)
    return SettingsV188(**defaults)


def _engine(settings: SettingsV188) -> ArbitrageEngineV186:
    engine = ArbitrageEngineV186(settings)
    engine.set_markets([_pair()])
    # Strong positive complete-set edge with ample depth.
    engine.books["A"].apply_snapshot(
        [{"price": "0.44", "size": "100"}],
        [{"price": "0.45", "size": "100"}],
    )
    engine.books["B"].apply_snapshot(
        [{"price": "0.44", "size": "100"}],
        [{"price": "0.45", "size": "100"}],
    )
    return engine


def _patch_live(monkeypatch) -> None:
    monkeypatch.setattr(raw187, "market_phase", lambda pair: MarketPhase.LIVE)
    monkeypatch.setattr(bfok187, "market_phase", lambda pair: MarketPhase.LIVE)


def test_freshness_defaults_are_controlled_frontier():
    settings = SettingsV188()
    assert settings.v188_freshness_frontier_enabled is True
    assert settings.v188_freshness_ages_ms == (25, 35, 50, 75, 100)
    assert settings.v188_freshness_size == Decimal("1")


def test_only_age_limit_changes_candidate_entry(tmp_path, monkeypatch):
    _patch_live(monkeypatch)
    settings = _settings()
    recorder = JsonlRecorder(str(tmp_path / "freshness.jsonl"))
    raw_suite = BatchFOKWithRawSuiteV187(settings, recorder)
    fresh_suite = FreshnessFrontierSuiteV188(settings, recorder)
    engine = _engine(settings)

    # Both books are ~40 ms old: 25/35 ms controls must block while
    # 50/75/100 ms variants see the exact same edge/coverage snapshot.
    old = time.monotonic() - 0.040
    engine.books["A"].updated_monotonic = old
    engine.books["B"].updated_monotonic = old
    surge = SimpleNamespace(active=False)

    raw_suite.on_market_update(engine, "fresh188", surge=surge)
    fresh_suite.on_market_update(
        engine,
        "fresh188",
        surge,
        raw_suite.last_raw_snapshot,
    )

    placements = {v.strategy: v.placements for v in fresh_suite.variants}
    assert placements["BFOK-FRESH25"] == 0
    assert placements["BFOK-FRESH35"] == 0
    assert placements["BFOK-FRESH50"] == 1
    assert placements["BFOK-FRESH75"] == 1
    assert placements["BFOK-FRESH100"] == 1

    # The protected settings other than max book age remain identical.
    for variant in fresh_suite.variants:
        assert variant.settings.v187_detection_min_edge_per_share == Decimal("0.005")
        assert variant.settings.v187_detection_coverage_multiple == Decimal("1.5")
        assert variant.settings.v187_use_surge_gate is True
        assert variant.settings.v187_batch_arrival_latency_ms == 1
        assert variant.settings.v187_recovery_latency_ms == 1
        assert variant.settings.v187_cooldown_ms == 250


def test_freshness_execution_records_detection_and_arrival_age(tmp_path, monkeypatch):
    _patch_live(monkeypatch)
    settings = _settings(v188_freshness_ages_ms=(50,))
    recorder = JsonlRecorder(str(tmp_path / "freshness.jsonl"))
    raw_suite = BatchFOKWithRawSuiteV187(settings, recorder)
    fresh_suite = FreshnessFrontierSuiteV188(settings, recorder)
    engine = _engine(settings)

    old = time.monotonic() - 0.030
    engine.books["A"].updated_monotonic = old
    engine.books["B"].updated_monotonic = old
    surge = SimpleNamespace(active=False)
    raw_suite.on_market_update(engine, "fresh188", surge=surge)
    fresh_suite.on_market_update(engine, "fresh188", surge, raw_suite.last_raw_snapshot)

    time.sleep(0.003)
    fresh_suite.process_due(engine)

    rows = [json.loads(line) for line in (tmp_path / "freshness.jsonl").read_text().splitlines()]
    executions = [r["payload"] for r in rows if r["event_type"] == "freshness_execution_v188"]
    assert len(executions) == 1
    event = executions[0]
    assert event["strategy"] == "BFOK-FRESH50"
    assert event["status"] == "BOTH_FILLED"
    assert Decimal(str(event["detected_older_book_age_ms"])) >= Decimal("25")
    assert Decimal(str(event["arrival_older_book_age_ms"])) >= Decimal(str(event["detected_older_book_age_ms"]))
    assert Decimal(str(event["arrival_market_edge_per_share"])) > 0
    assert Decimal(str(event["realized_pnl"])) > 0


def test_runtime_raw_win_age_uses_strategy_equity_counter(tmp_path, monkeypatch):
    """Regression: runtime must not reference a nonexistent raw.wins field.

    The first v1.8.8 build did exactly that after RAW execution. The resulting
    AttributeError happened before the freshness suite was called, which made
    every BFOK-FRESH row remain IDLE and suppressed raw_win_age_v188 events.
    """
    _patch_live(monkeypatch)
    settings = _settings(v188_freshness_ages_ms=(50,))
    recorder = JsonlRecorder(str(tmp_path / "runtime.jsonl"))
    raw_suite = BatchFOKWithRawSuiteV187(settings, recorder)
    fresh_suite = FreshnessFrontierSuiteV188(settings, recorder)
    engine = _engine(settings)
    surge = SimpleNamespace(active=False)

    raw_suite.on_market_update(engine, "fresh188", surge=surge)
    assert raw_suite.raw is not None
    assert raw_suite.raw.equity.wins == 1

    runtime = object.__new__(BatchFirstMakerResearchSuiteV188)
    runtime.settings = settings
    runtime.recorder = recorder
    runtime.batch = raw_suite
    runtime.freshness = fresh_suite
    runtime._record_raw_win_age(
        engine,
        "fresh188",
        before_wins=0,
        before_equity=Decimal("0"),
    )

    rows = [json.loads(line) for line in (tmp_path / "runtime.jsonl").read_text().splitlines()]
    age_rows = [r["payload"] for r in rows if r["event_type"] == "raw_win_age_v188"]
    assert len(age_rows) == 1
    assert age_rows[0]["source_strategy"] == "BFOK-RAW"
    assert Decimal(str(age_rows[0]["raw_pnl"])) > 0
    assert age_rows[0]["older_book_age_ms"] is not None


def test_low_latency_recorder_does_not_persist_per_attempt_raw_events(tmp_path, monkeypatch):
    session = tmp_path / "session.jsonl"
    main = tmp_path / "main.jsonl"
    monkeypatch.setenv("V186_CURRENT_SESSION_PATH", str(session))
    recorder = LowLatencyJsonlRecorderV186(str(main))
    recorder.write(
        "strategy_equity",
        {"strategy": "BFOK-RAW", "pnl_delta": "-0.05"},
    )
    recorder.write(
        "batch_fok_execution_summary_v187",
        {"strategy": "BFOK-RAW", "realized_pnl": "-0.05"},
    )
    recorder.write(
        "raw_win_age_v188",
        {"source_strategy": "BFOK-RAW", "raw_pnl": "0.03"},
    )
    recorder.close()

    rows = [json.loads(line) for line in main.read_text().splitlines()]
    assert [row["event_type"] for row in rows] == ["raw_win_age_v188"]
    session_rows = [json.loads(line) for line in session.read_text().splitlines()]
    assert [row["event_type"] for row in session_rows] == ["raw_win_age_v188"]
