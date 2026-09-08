from __future__ import annotations

import json
import time
from decimal import Decimal

import arb_bot.atomic_benchmark as atomic_base
import arb_bot.maker_research as maker_base
from arb_bot.atomic_benchmark_v184 import ExecutableAtomicVariantV184
from arb_bot.config_v184 import SettingsV184
from arb_bot.maker_research import MarketRegimeTracker, SurgeSnapshot
from arb_bot.models import MarketPair
from arb_bot.selective_research_v184 import SelectivePairedMakerVariantV184
from arb_bot.storage import JsonlRecorder
from arb_bot.strategy import ArbitrageEngine


def _pair() -> MarketPair:
    return MarketPair(
        market_id="m-live",
        condition_id="c-live",
        slug="doge-updown-15m-1999999800",
        question="DOGE Up or Down 15m",
        outcome_a="Up",
        outcome_b="Down",
        token_a="A",
        token_b="B",
    )


def _engine(settings) -> ArbitrageEngine:
    engine = ArbitrageEngine(settings)
    engine.set_markets([_pair()])
    engine.books["A"].apply_snapshot(
        [{"price": "0.40", "size": "6"}],
        [{"price": "0.41", "size": "20"}],
    )
    engine.books["B"].apply_snapshot(
        [{"price": "0.57", "size": "6"}],
        [{"price": "0.58", "size": "20"}],
    )
    return engine


def _events(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_v184_zero_latency_cancel_beats_future_fill(tmp_path, monkeypatch):
    monkeypatch.setattr(maker_base, "market_phase", lambda pair: maker_base.MarketPhase.LIVE)
    settings = SettingsV184(
        maker_min_seconds_to_expiry=0,
        maker_use_empirical_risk_gate=False,
        v184_cancel_latency_ms=0,
    )
    path = tmp_path / "cancel.jsonl"
    variant = SelectivePairedMakerVariantV184(
        settings,
        JsonlRecorder(str(path)),
        MarketRegimeTracker(settings),
        max_pair=Decimal("0.97"),
        max_queue=Decimal("10"),
    )
    engine = _engine(settings)

    variant.on_market_update(engine, "m-live", SurgeSnapshot(False, ()))
    campaign = variant.campaigns["m-live"]

    # A-first completion is now toxic: maker A=0.40 plus B taker ask=0.63.
    engine.books["B"].apply_snapshot(
        [{"price": "0.57", "size": "6"}],
        [{"price": "0.63", "size": "20"}],
    )
    variant._observe_ghost_prefill_gates(engine, _pair(), campaign)
    assert "m-live" in variant._v184_pending_cancel

    variant.process_due(engine)
    assert "m-live" not in variant.campaigns

    rows = _events(path)
    assert any(row["event_type"] == "maker_variant_fast_cancel_intent_v184" for row in rows)
    assert any(row["event_type"] == "maker_variant_fast_cancel_effective_v184" for row in rows)
    assert not any(row["event_type"] == "maker_variant_fast_cancel_race_lost_v184" for row in rows)


def test_v184_fill_can_beat_slow_cancel(tmp_path, monkeypatch):
    monkeypatch.setattr(maker_base, "market_phase", lambda pair: maker_base.MarketPhase.LIVE)
    settings = SettingsV184(
        maker_min_seconds_to_expiry=0,
        maker_use_empirical_risk_gate=False,
        v184_cancel_latency_ms=100,
    )
    path = tmp_path / "race.jsonl"
    variant = SelectivePairedMakerVariantV184(
        settings,
        JsonlRecorder(str(path)),
        MarketRegimeTracker(settings),
        max_pair=Decimal("0.97"),
        max_queue=Decimal("10"),
    )
    engine = _engine(settings)

    variant.on_market_update(engine, "m-live", SurgeSnapshot(False, ()))
    campaign = variant.campaigns["m-live"]
    engine.books["B"].apply_snapshot(
        [{"price": "0.57", "size": "6"}],
        [{"price": "0.63", "size": "20"}],
    )
    variant._observe_ghost_prefill_gates(engine, _pair(), campaign)
    assert "m-live" in variant._v184_pending_cancel

    engine.books["A"].apply_trade("0.40", "7", "SELL")
    variant.on_market_update(engine, "m-live", SurgeSnapshot(False, ()))
    assert variant.campaigns["m-live"].first_fill_at is not None

    rows = _events(path)
    race = [row for row in rows if row["event_type"] == "maker_variant_fast_cancel_race_lost_v184"]
    assert len(race) == 1
    assert race[0]["payload"]["reason"] == "TOXIC_EDGE"
    assert any(row["event_type"] == "maker_variant_prefill_timeline_v184" for row in rows)


def test_v184_atomic_proxy_requotes_full_size_after_latency(tmp_path, monkeypatch):
    monkeypatch.setattr(atomic_base, "market_phase", lambda pair: atomic_base.MarketPhase.LIVE)
    settings = SettingsV184(
        atomic_sizes=(Decimal("1"),),
        v184_atomic_execution_latencies_ms=(0,),
        v184_atomic_max_book_age_ms=1000,
        v184_atomic_min_execution_edge_per_share=Decimal("0.0001"),
    )
    path = tmp_path / "atomic.jsonl"
    variant = ExecutableAtomicVariantV184(
        settings,
        JsonlRecorder(str(path)),
        shares=Decimal("1"),
        direction="BUY_PAIR",
    )
    engine = ArbitrageEngine(settings)
    engine.set_markets([_pair()])
    engine.books["A"].apply_snapshot(
        [{"price": "0.39", "size": "20"}],
        [{"price": "0.40", "size": "20"}],
    )
    engine.books["B"].apply_snapshot(
        [{"price": "0.49", "size": "20"}],
        [{"price": "0.50", "size": "20"}],
    )

    variant.on_market_update(engine, "m-live")
    assert "m-live" in variant.active
    variant._v184_process_execution_deadlines(engine, time.monotonic())

    rows = _events(path)
    attempts = [row for row in rows if row["event_type"] == "atomic_execution_proxy_v184"]
    assert len(attempts) == 1
    assert attempts[0]["payload"]["outcome"] == "EXECUTABLE_SHADOW_FILL"
    assert Decimal(attempts[0]["payload"]["pnl"]) > 0
