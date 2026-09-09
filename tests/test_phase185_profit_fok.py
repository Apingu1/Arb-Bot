from __future__ import annotations

import json
import time
from decimal import Decimal

import arb_bot.profit_fok_v185 as pfok_mod
from arb_bot.config_v185 import SettingsV185
from arb_bot.models import MarketPair
from arb_bot.profit_fok_v185 import ProfitFOKEngineV185
from arb_bot.storage import JsonlRecorder
from arb_bot.strategy import ArbitrageEngine


def _pair() -> MarketPair:
    return MarketPair(
        market_id="m-live",
        condition_id="c-live",
        slug="btc-updown-15m-1999999800",
        question="BTC Up or Down 15m",
        outcome_a="Up",
        outcome_b="Down",
        token_a="A",
        token_b="B",
    )


def _engine(settings) -> ArbitrageEngine:
    engine = ArbitrageEngine(settings)
    engine.set_markets([_pair()])
    engine.books["A"].apply_snapshot(
        [{"price": "0.38", "size": "20"}],
        [{"price": "0.40", "size": "20"}],
    )
    engine.books["B"].apply_snapshot(
        [{"price": "0.48", "size": "20"}],
        [{"price": "0.50", "size": "20"}],
    )
    return engine


def _events(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def _settings(**kwargs):
    defaults = dict(
        v185_profit_sizes=(Decimal("1"), Decimal("2"), Decimal("5")),
        v185_detection_min_edge_per_share=Decimal("0.015"),
        v185_preflight_min_edge_per_share=Decimal("0.010"),
        v185_final_min_edge_per_share=Decimal("0.005"),
        v185_detection_coverage_multiple=Decimal("3"),
        v185_preflight_coverage_multiple=Decimal("1.5"),
        v185_max_book_age_ms=1000,
        v185_base_latency_ms=0,
        v185_leg_gap_ms=1,
        v185_recovery_latency_ms=0,
        v185_cooldown_ms=0,
        v185_use_surge_gate=False,
    )
    defaults.update(kwargs)
    return SettingsV185(**defaults)


def test_v185_books_real_positive_pfok_trade(tmp_path, monkeypatch):
    monkeypatch.setattr(pfok_mod, "market_phase", lambda pair: pfok_mod.MarketPhase.LIVE)
    settings = _settings()
    path = tmp_path / "profit.jsonl"
    strategy = ProfitFOKEngineV185(settings, JsonlRecorder(str(path)))
    engine = _engine(settings)

    strategy.on_market_update(engine, "m-live")
    pending = strategy.pending["m-live"]
    assert pending.shares == Decimal("5")

    strategy.process_due(engine)
    pending = strategy.pending["m-live"]
    assert pending.first_fill is not None
    assert pending.second_fill is None

    pending.second_due = time.monotonic() - 1
    strategy.process_due(engine)

    assert "m-live" not in strategy.pending
    assert strategy.equity.realized_events == 1
    assert strategy.equity.wins == 1
    assert strategy.equity.equity > 0

    rows = _events(path)
    summary = next(
        row["payload"]
        for row in rows
        if row["event_type"] == "dual_fok_execution_summary"
    )
    assert summary["strategy"] == "PFOK"
    assert summary["status"] == "BOTH_FILLED"
    assert Decimal(summary["realized_pnl"]) > 0
    assert Decimal(summary["preflight_edge_per_share"]) >= Decimal("0.010")


def test_v185_preflight_failure_is_no_trade_not_flat_win(tmp_path, monkeypatch):
    monkeypatch.setattr(pfok_mod, "market_phase", lambda pair: pfok_mod.MarketPhase.LIVE)
    settings = _settings()
    path = tmp_path / "reject.jsonl"
    strategy = ProfitFOKEngineV185(settings, JsonlRecorder(str(path)))
    engine = _engine(settings)

    strategy.on_market_update(engine, "m-live")
    engine.books["B"].apply_snapshot(
        [{"price": "0.48", "size": "20"}],
        [{"price": "0.70", "size": "20"}],
    )
    strategy.process_due(engine)

    assert "m-live" not in strategy.pending
    assert strategy.preflight_rejects == 1
    assert strategy.equity.realized_events == 0
    assert strategy.equity.equity == 0

    rows = _events(path)
    assert any(row["event_type"] == "profit_fok_preflight_reject_v185" for row in rows)
    assert not any(row["event_type"] == "dual_fok_execution_summary" for row in rows)
    assert not any(row["event_type"] == "strategy_equity" for row in rows)


def test_v185_one_leg_miss_books_recovery_loss_honestly(tmp_path, monkeypatch):
    monkeypatch.setattr(pfok_mod, "market_phase", lambda pair: pfok_mod.MarketPhase.LIVE)
    settings = _settings()
    path = tmp_path / "miss.jsonl"
    strategy = ProfitFOKEngineV185(settings, JsonlRecorder(str(path)))
    engine = _engine(settings)

    strategy.on_market_update(engine, "m-live")
    strategy.process_due(engine)
    pending = strategy.pending["m-live"]
    assert pending.first_leg == "A"
    assert pending.first_fill is not None

    # The second leg disappears after the first leg is already filled. The
    # strategy must not erase the exposure; it must recover and book the loss.
    engine.books["A"].apply_snapshot(
        [{"price": "0.30", "size": "20"}],
        [{"price": "0.40", "size": "20"}],
    )
    engine.books["B"].apply_snapshot(
        [{"price": "0.48", "size": "20"}],
        [{"price": "0.70", "size": "20"}],
    )
    pending.second_due = time.monotonic() - 1
    strategy.process_due(engine)
    pending = strategy.pending["m-live"]
    assert pending.recovery_due is not None
    pending.recovery_due = time.monotonic() - 1
    strategy.process_due(engine)

    assert strategy.equity.realized_events == 1
    assert strategy.equity.losses == 1
    assert strategy.equity.equity < 0
    assert strategy.one_leg_miss == 1

    rows = _events(path)
    summary = next(
        row["payload"]
        for row in rows
        if row["event_type"] == "dual_fok_execution_summary"
    )
    assert summary["status"] == "ONE_LEG_MISS"
    assert Decimal(summary["realized_pnl"]) < 0
