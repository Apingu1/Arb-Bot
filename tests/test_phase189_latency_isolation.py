from __future__ import annotations

import json
import time
from decimal import Decimal

import arb_bot.batch_fok_v187 as bfok187
import arb_bot.raw_observer_v189 as raw189
from arb_bot.config_v189 import SettingsV189
from arb_bot.discovery import MarketPhase
from arb_bot.latency_frontier_v189 import LatencyFrontierSuiteV189
from arb_bot.models import MarketPair
from arb_bot.raw_observer_v189 import RawOpportunityObserverV189
from arb_bot.runtime_v189 import ProtectedBatchSuiteV189
from arb_bot.storage import JsonlRecorder
from arb_bot.strategy_v186 import ArbitrageEngineV186


def _pair() -> MarketPair:
    return MarketPair(
        market_id="m189",
        condition_id="c189",
        slug="sol-updown-15m-1999999800",
        question="SOL Up or Down 15m",
        outcome_a="Up",
        outcome_b="Down",
        token_a="A",
        token_b="B",
    )


def _settings(**kwargs) -> SettingsV189:
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
        v187_raw_enabled=False,
        v188_freshness_frontier_enabled=False,
        v189_latency_frontier_enabled=True,
        v189_latency_targets_ms=(0, 1, 2, 3, 5),
        v189_latency_size=Decimal("1"),
        v189_raw_observer_enabled=True,
        v189_raw_observer_rollup_seconds=5,
    )
    defaults.update(kwargs)
    return SettingsV189(**defaults)


def _engine(settings: SettingsV189, *, ask_a="0.45", ask_b="0.45") -> ArbitrageEngineV186:
    engine = ArbitrageEngineV186(settings)
    engine.set_markets([_pair()])
    engine.books["A"].apply_snapshot(
        [{"price": "0.44", "size": "100"}],
        [{"price": ask_a, "size": "100"}],
    )
    engine.books["B"].apply_snapshot(
        [{"price": "0.44", "size": "100"}],
        [{"price": ask_b, "size": "100"}],
    )
    return engine


def _patch_live(monkeypatch) -> None:
    monkeypatch.setattr(bfok187, "market_phase", lambda pair: MarketPhase.LIVE)
    monkeypatch.setattr(raw189, "market_phase", lambda pair: MarketPhase.LIVE)


def test_v189_defaults_remove_full_raw_and_freshness_load():
    settings = SettingsV189()
    assert settings.v187_raw_enabled is False
    assert settings.v188_freshness_frontier_enabled is False
    assert settings.v189_latency_frontier_enabled is True
    assert settings.v189_latency_targets_ms == (0, 1, 2, 3, 5)
    assert settings.v189_latency_size == Decimal("1")
    assert settings.v189_raw_observer_enabled is True


def test_v189_raw_observer_does_not_persist_negative_attempts(tmp_path, monkeypatch):
    _patch_live(monkeypatch)
    settings = _settings()
    path = tmp_path / "rawobs.jsonl"
    recorder = JsonlRecorder(str(path))
    observer = RawOpportunityObserverV189(settings, recorder)
    engine = _engine(settings, ask_a="0.55", ask_b="0.55")

    observer.on_market_update(engine, "m189")
    observer.force_rollup()

    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert not any(row["event_type"] == "raw_positive_observation_v189" for row in rows)
    assert not any(row["event_type"] == "strategy_equity" for row in rows)
    rollup = next(row["payload"] for row in rows if row["event_type"] == "raw_observer_rollup_v189")
    assert rollup["counts"]["NONPOSITIVE"] == 1

    # A positive observation gets one sparse detailed event, still with no
    # StrategyEquity/fill lifecycle.
    engine.books["A"].apply_snapshot(
        [{"price": "0.44", "size": "100"}],
        [{"price": "0.45", "size": "100"}],
    )
    engine.books["B"].apply_snapshot(
        [{"price": "0.44", "size": "100"}],
        [{"price": "0.45", "size": "100"}],
    )
    observer.on_market_update(engine, "m189")
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    positive = [row["payload"] for row in rows if row["event_type"] == "raw_positive_observation_v189"]
    assert len(positive) == 1
    assert positive[0]["observation_only"] is True
    assert Decimal(str(positive[0]["pnl_upper_bound"])) > 0
    assert not any(row["event_type"] == "strategy_equity" for row in rows)


def test_v189_latency_frontier_varies_only_target_and_books_profit(tmp_path, monkeypatch):
    _patch_live(monkeypatch)
    settings = _settings(v187_use_surge_gate=False, v187_cooldown_ms=0)
    path = tmp_path / "latency.jsonl"
    recorder = JsonlRecorder(str(path))
    batch = ProtectedBatchSuiteV189(settings, recorder)
    latency = LatencyFrontierSuiteV189(settings, recorder)
    engine = _engine(settings)

    snapshot = batch.base._build_snapshot(engine, "m189")
    assert snapshot is not None and snapshot.metrics.get(Decimal("1")) is not None
    latency.on_market_update(engine, "m189", surge=None, snapshot=snapshot)

    # LAT0 completes in the same callback. The scheduled variants complete once
    # their target deadlines pass.
    lat0 = next(v for v in latency.variants if v.target_ms == 0)
    assert lat0.completed == 1
    assert lat0.wins == 1
    assert lat0.equity > 0

    time.sleep(0.008)
    latency.process_due(engine)
    for variant in latency.variants:
        assert variant.candidates == 1
        assert variant.completed == 1
        assert variant.both_filled == 1
        assert variant.wins == 1
        assert variant.losses == 0
        assert variant.equity > 0

    rows = [json.loads(line) for line in path.read_text().splitlines()]
    executions = [row["payload"] for row in rows if row["event_type"] == "latency_execution_v189"]
    assert {p["strategy"] for p in executions} == {
        "BFOK-LAT0",
        "BFOK-LAT1",
        "BFOK-LAT2",
        "BFOK-LAT3",
        "BFOK-LAT5",
    }
    assert all(p["protected_detection"] is True for p in executions)
    assert all(p["atomic"] is False for p in executions)
    assert all(Decimal(str(p["arrival_market_edge_per_share"])) > 0 for p in executions)


def test_v189_latency_frontier_respects_25ms_protected_freshness(tmp_path, monkeypatch):
    _patch_live(monkeypatch)
    settings = _settings(v187_use_surge_gate=False)
    recorder = JsonlRecorder(str(tmp_path / "stale.jsonl"))
    batch = ProtectedBatchSuiteV189(settings, recorder)
    latency = LatencyFrontierSuiteV189(settings, recorder)
    engine = _engine(settings)

    old = time.monotonic() - 0.100
    engine.books["A"].updated_monotonic = old
    engine.books["B"].updated_monotonic = old
    snapshot = batch.base._build_snapshot(engine, "m189")
    assert snapshot is not None
    assert snapshot.metrics == {}

    latency.on_market_update(engine, "m189", surge=None, snapshot=snapshot)
    assert all(v.candidates == 0 for v in latency.variants)
