from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

import arb_bot.batch_fok_raw_v187 as raw187
import arb_bot.batch_fok_v187 as bfok187
import arb_bot.opportunity_funnel_v187 as funnel187
import arb_bot.profit_fok_v185 as pfok185
from arb_bot.batch_fok_raw_v187 import BatchFOKWithRawSuiteV187
from arb_bot.config_v187_raw import SettingsV187Raw
from arb_bot.discovery import MarketPhase
from arb_bot.models import MarketPair
from arb_bot.opportunity_funnel_v187 import OpportunityFunnelV187
from arb_bot.runtime_v187 import CorePFOKSuiteV187
from arb_bot.storage import JsonlRecorder
from arb_bot.strategy_v186 import ArbitrageEngineV186


def _pair() -> MarketPair:
    return MarketPair(
        market_id="funnel187",
        condition_id="cfunnel187",
        slug="hype-updown-15m-1999999800",
        question="HYPE Up or Down 15m",
        outcome_a="Up",
        outcome_b="Down",
        token_a="A",
        token_b="B",
    )


def _settings(**kwargs) -> SettingsV187Raw:
    defaults = dict(
        v181_selective_enabled=False,
        v185_profit_fok_enabled=True,
        v185_profit_sizes=(Decimal("1"),),
        v185_detection_min_edge_per_share=Decimal("0.005"),
        v185_detection_coverage_multiple=Decimal("1.5"),
        v185_max_book_age_ms=1000,
        v185_base_latency_ms=2,
        v185_leg_gap_ms=1,
        v185_recovery_latency_ms=1,
        v185_cooldown_ms=250,
        v185_use_surge_gate=True,
        v185_frontier_gate_sample_interval_ms=1000,
        v187_batch_fok_enabled=True,
        v187_batch_sizes=(Decimal("1"),),
        v187_detection_min_edge_per_share=Decimal("0.005"),
        v187_detection_coverage_multiple=Decimal("1.5"),
        v187_max_book_age_ms=1000,
        v187_batch_arrival_latency_ms=1,
        v187_recovery_latency_ms=1,
        v187_cooldown_ms=250,
        v187_use_surge_gate=True,
        v187_ev_enabled=False,
        v187_raw_enabled=True,
        v187_raw_size=Decimal("1"),
        v187_funnel_enabled=True,
        v187_funnel_rollup_seconds=1,
        v187_keep_pfok_size_controls=True,
    )
    defaults.update(kwargs)
    return SettingsV187Raw(**defaults)


def _engine(settings: SettingsV187Raw) -> ArbitrageEngineV186:
    engine = ArbitrageEngineV186(settings)
    engine.set_markets([_pair()])
    engine.books["A"].apply_snapshot(
        [{"price": "0.35", "size": "100"}],
        [{"price": "0.40", "size": "100"}],
    )
    engine.books["B"].apply_snapshot(
        [{"price": "0.45", "size": "100"}],
        [{"price": "0.50", "size": "100"}],
    )
    return engine


def _patch_live(monkeypatch) -> None:
    monkeypatch.setattr(raw187, "market_phase", lambda pair: MarketPhase.LIVE)
    monkeypatch.setattr(bfok187, "market_phase", lambda pair: MarketPhase.LIVE)
    monkeypatch.setattr(funnel187, "market_phase", lambda pair: MarketPhase.LIVE)
    monkeypatch.setattr(pfok185, "market_phase", lambda pair: MarketPhase.LIVE)


def test_raw_win_records_that_protected_models_were_blocked_by_surge(tmp_path, monkeypatch):
    _patch_live(monkeypatch)
    settings = _settings()
    recorder = JsonlRecorder(str(tmp_path / "funnel.jsonl"))
    batch = BatchFOKWithRawSuiteV187(settings, recorder)
    control = CorePFOKSuiteV187(settings, recorder)
    funnel = OpportunityFunnelV187(settings, recorder)
    funnel._last_rollup = 0.0
    engine = _engine(settings)
    surge = SimpleNamespace(active=True)

    funnel.begin_update(engine, "funnel187", surge, batch)
    batch.on_market_update(engine, "funnel187", surge)
    funnel.after_batch("funnel187", batch)
    funnel.before_pfok(engine, "funnel187", surge, control)
    control.on_market_update(engine, "funnel187", surge)
    funnel.after_pfok("funnel187", control)

    rows = [json.loads(line) for line in (tmp_path / "funnel.jsonl").read_text().splitlines()]
    attribution = next(row["payload"] for row in rows if row["event_type"] == "raw_win_attribution_v187")
    assert Decimal(str(attribution["raw_pnl"])) > 0
    assert attribution["all_protected_blocked"] is True
    assert attribution["protected_models"]["BFOK-1"] == {"state": "BLOCKED", "reason": "SURGE"}
    assert attribution["protected_models"]["PFOK"] == {"state": "BLOCKED", "reason": "SURGE"}
    assert attribution["protected_models"]["PFOK-S10"] == {"state": "BLOCKED", "reason": "SURGE"}
    assert attribution["protected_models"]["PFOK-S20"] == {"state": "BLOCKED", "reason": "SURGE"}

    rollup = next(row["payload"] for row in rows if row["event_type"] == "opportunity_funnel_rollup_v187")
    assert rollup["counts"]["MARKET_UPDATES"] == 1
    assert rollup["counts"]["FULL_RAW_SIZE_PAIR"] == 1
    assert rollup["counts"]["EDGE_GE_005"] == 1
    assert rollup["counts"]["RAW_WINS"] == 1
    assert rollup["counts"]["RAW_WINS_BLOCKED_BY_ALL_PROTECTED"] == 1


def test_raw_win_records_protected_entry_when_gate_allows_it(tmp_path, monkeypatch):
    _patch_live(monkeypatch)
    settings = _settings(v185_use_surge_gate=False, v187_use_surge_gate=False)
    recorder = JsonlRecorder(str(tmp_path / "entry.jsonl"))
    batch = BatchFOKWithRawSuiteV187(settings, recorder)
    control = CorePFOKSuiteV187(settings, recorder)
    funnel = OpportunityFunnelV187(settings, recorder)
    engine = _engine(settings)

    funnel.begin_update(engine, "funnel187", None, batch)
    batch.on_market_update(engine, "funnel187", None)
    funnel.after_batch("funnel187", batch)
    funnel.before_pfok(engine, "funnel187", None, control)
    control.on_market_update(engine, "funnel187", None)
    funnel.after_pfok("funnel187", control)

    rows = [json.loads(line) for line in (tmp_path / "entry.jsonl").read_text().splitlines()]
    attribution = next(row["payload"] for row in rows if row["event_type"] == "raw_win_attribution_v187")
    assert attribution["protected_models"]["BFOK-1"]["state"] == "SUBMITTED"
    assert attribution["protected_models"]["PFOK"]["state"] == "CANDIDATE"
    assert attribution["all_protected_blocked"] is False
