from __future__ import annotations

import json
from decimal import Decimal

import arb_bot.batch_fok_v187 as bfok187
from arb_bot.batch_fok_v187 import PreciseBatchFOKSuiteV187
from arb_bot.config_v187 import SettingsV187
from arb_bot.discovery import MarketPhase
from arb_bot.models import MarketPair
from arb_bot.runtime_v187 import CorePFOKSuiteV187
from arb_bot.storage import JsonlRecorder
from arb_bot.strategy_v186 import ArbitrageEngineV186


def _pair() -> MarketPair:
    return MarketPair(
        market_id="m187",
        condition_id="c187",
        slug="sol-updown-15m-1999999800",
        question="SOL Up or Down 15m",
        outcome_a="Up",
        outcome_b="Down",
        token_a="A",
        token_b="B",
    )


def _settings(**kwargs) -> SettingsV187:
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
        v185_base_latency_ms=0,
        v185_leg_gap_ms=0,
        v185_recovery_latency_ms=0,
        v185_cooldown_ms=0,
        v185_use_surge_gate=False,
        v187_batch_fok_enabled=True,
        v187_batch_sizes=(Decimal("1"), Decimal("5"), Decimal("10"), Decimal("20")),
        v187_detection_min_edge_per_share=Decimal("0.005"),
        v187_final_min_edge_per_share=Decimal("0.001"),
        v187_detection_coverage_multiple=Decimal("1.5"),
        v187_max_book_age_ms=1000,
        v187_batch_arrival_latency_ms=0,
        v187_recovery_latency_ms=0,
        v187_cooldown_ms=0,
        v187_use_surge_gate=False,
        v187_ev_enabled=True,
        v187_ev_sizes=(Decimal("1"), Decimal("5"), Decimal("10"), Decimal("20")),
        v187_ev_prior_both_probability=Decimal("0.80"),
        v187_ev_prior_weight=Decimal("8"),
        v187_ev_prior_miss_loss_per_share=Decimal("0.015"),
        v187_ev_min_expected_pnl=Decimal("0.001"),
        v187_keep_pfok_size_controls=True,
    )
    defaults.update(kwargs)
    return SettingsV187(**defaults)


def _engine(settings: SettingsV187) -> ArbitrageEngineV186:
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


def _variant(suite: PreciseBatchFOKSuiteV187, name: str):
    return next(v for v in suite.variants if v.strategy == name)


def test_v187_keeps_only_requested_pfok_core_controls(tmp_path):
    settings = _settings()
    recorder = JsonlRecorder(str(tmp_path / "pfok.jsonl"))
    suite = CorePFOKSuiteV187(settings, recorder)
    names = [row["strategy"] for row in suite.diagnostic_rows()]
    assert names == ["PFOK", "PFOK-S10", "PFOK-S20"]


def test_v187_batch_suite_contains_fixed_sizes_and_ev(tmp_path):
    settings = _settings()
    recorder = JsonlRecorder(str(tmp_path / "bfok.jsonl"))
    suite = PreciseBatchFOKSuiteV187(settings, recorder)
    names = [row["strategy"] for row in suite.diagnostic_rows()]
    assert names == ["BFOK-1", "BFOK-5", "BFOK-10", "BFOK-20", "BFOK-EV"]


def test_v187_parallel_batch_both_fill_books_profit(tmp_path, monkeypatch):
    monkeypatch.setattr(bfok187, "market_phase", lambda pair: MarketPhase.LIVE)
    settings = _settings(v187_ev_enabled=False)
    recorder = JsonlRecorder(str(tmp_path / "both.jsonl"))
    suite = PreciseBatchFOKSuiteV187(settings, recorder)
    engine = _engine(settings)

    suite.on_market_update(engine, "m187", surge=None)
    suite.process_due(engine)

    v = _variant(suite, "BFOK-5")
    assert v.placements == 1
    assert v.both_filled == 1
    assert v.one_leg_miss == 0
    assert v.equity.equity > 0

    rows = [json.loads(line) for line in (tmp_path / "both.jsonl").read_text().splitlines()]
    summary = next(
        row["payload"]
        for row in rows
        if row["event_type"] == "batch_fok_execution_summary_v187"
        and row["payload"]["strategy"] == "BFOK-5"
    )
    assert summary["status"] == "BOTH_FILLED"
    assert summary["batch_parallel"] is True
    assert summary["atomic"] is False
    assert summary["leg_a_filled"] is True
    assert summary["leg_b_filled"] is True


def test_v187_parallel_batch_can_one_leg_miss_and_recover(tmp_path, monkeypatch):
    monkeypatch.setattr(bfok187, "market_phase", lambda pair: MarketPhase.LIVE)
    settings = _settings(v187_ev_enabled=False)
    recorder = JsonlRecorder(str(tmp_path / "miss.jsonl"))
    suite = PreciseBatchFOKSuiteV187(settings, recorder)
    engine = _engine(settings)

    suite.on_market_update(engine, "m187", surge=None)

    # A remains inside its submitted FOK limit while B moves far beyond the
    # submitted limit. Parallel execution must therefore model A-fill/B-miss,
    # not grant an artificial atomic complete-set fill.
    engine.books["B"].apply_snapshot(
        [{"price": "0.68", "size": "100"}],
        [{"price": "0.70", "size": "100"}],
    )
    suite.process_due(engine)
    suite.process_due(engine)

    v = _variant(suite, "BFOK-5")
    assert v.both_filled == 0
    assert v.one_leg_miss == 1
    assert v.equity.equity < 0

    rows = [json.loads(line) for line in (tmp_path / "miss.jsonl").read_text().splitlines()]
    summary = next(
        row["payload"]
        for row in rows
        if row["event_type"] == "batch_fok_execution_summary_v187"
        and row["payload"]["strategy"] == "BFOK-5"
    )
    assert summary["status"] == "ONE_LEG_MISS"
    assert summary["leg_a_filled"] is True
    assert summary["leg_b_filled"] is False
    assert Decimal(str(summary["realized_pnl"])) < 0


def test_v187_ev_gate_can_reject_positive_raw_edge_when_miss_risk_is_bad(tmp_path, monkeypatch):
    monkeypatch.setattr(bfok187, "market_phase", lambda pair: MarketPhase.LIVE)
    settings = _settings(
        v187_ev_prior_both_probability=Decimal("0.20"),
        v187_ev_prior_weight=Decimal("20"),
        v187_ev_prior_miss_loss_per_share=Decimal("0.05"),
        v187_ev_min_expected_pnl=Decimal("0.001"),
    )
    recorder = JsonlRecorder(str(tmp_path / "ev.jsonl"))
    suite = PreciseBatchFOKSuiteV187(settings, recorder)
    engine = _engine(settings)

    suite.on_market_update(engine, "m187", surge=None)

    fixed = _variant(suite, "BFOK-5")
    ev = _variant(suite, "BFOK-EV")
    assert fixed.placements == 1
    assert ev.placements == 0
    assert ev.ev_rejects == 1

    rows = [json.loads(line) for line in (tmp_path / "ev.jsonl").read_text().splitlines()]
    gates = [
        row["payload"]
        for row in rows
        if row["event_type"] == "batch_fok_ev_gate_v187"
    ]
    assert gates
    assert all(not gate["passed"] for gate in gates)


def test_v187_fixed_control_outcomes_feed_empirical_risk_book(tmp_path, monkeypatch):
    monkeypatch.setattr(bfok187, "market_phase", lambda pair: MarketPhase.LIVE)
    settings = _settings(v187_ev_enabled=False)
    recorder = JsonlRecorder(str(tmp_path / "risk.jsonl"))
    suite = PreciseBatchFOKSuiteV187(settings, recorder)
    engine = _engine(settings)

    suite.on_market_update(engine, "m187", surge=None)
    suite.process_due(engine)

    metrics = suite._build_snapshot(engine, "m187").metrics[Decimal("5")]
    estimate = suite.risk_book.estimate(
        asset="SOL",
        shares=Decimal("5"),
        detected_edge=metrics["edge"],
        detected_pnl=metrics["pnl"],
    )
    assert estimate["samples"] == 1
    assert estimate["both_filled"] == 1
