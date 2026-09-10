from __future__ import annotations

import json
import time
from decimal import Decimal
from types import SimpleNamespace

import arb_bot.batch_fok_raw_v187 as raw187
import arb_bot.batch_fok_v187 as bfok187
from arb_bot.batch_fok_raw_v187 import BatchFOKWithRawSuiteV187
from arb_bot.config_v187_raw import SettingsV187Raw
from arb_bot.discovery import MarketPhase
from arb_bot.models import MarketPair
from arb_bot.storage import JsonlRecorder
from arb_bot.strategy_v186 import ArbitrageEngineV186


def _pair() -> MarketPair:
    return MarketPair(
        market_id="raw187",
        condition_id="craw187",
        slug="sol-updown-15m-1999999800",
        question="SOL Up or Down 15m",
        outcome_a="Up",
        outcome_b="Down",
        token_a="A",
        token_b="B",
    )


def _settings(**kwargs) -> SettingsV187Raw:
    defaults = dict(
        v181_selective_enabled=False,
        v187_batch_fok_enabled=True,
        v187_batch_sizes=(Decimal("5"),),
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
    )
    defaults.update(kwargs)
    return SettingsV187Raw(**defaults)


def _engine(settings: SettingsV187Raw) -> ArbitrageEngineV186:
    engine = ArbitrageEngineV186(settings)
    engine.set_markets([_pair()])
    engine.books["A"].apply_snapshot(
        [{"price": "0.50", "size": "100"}],
        [{"price": "0.55", "size": "100"}],
    )
    engine.books["B"].apply_snapshot(
        [{"price": "0.50", "size": "100"}],
        [{"price": "0.55", "size": "100"}],
    )
    return engine


def test_raw_defaults_are_enabled_and_one_share():
    settings = SettingsV187Raw()
    assert settings.v187_raw_enabled is True
    assert settings.v187_raw_size == Decimal("1")


def test_raw_trades_negative_edge_stale_surge_state_with_no_cooldown(tmp_path, monkeypatch):
    monkeypatch.setattr(raw187, "market_phase", lambda pair: MarketPhase.LIVE)
    monkeypatch.setattr(bfok187, "market_phase", lambda pair: MarketPhase.LIVE)

    settings = _settings()
    recorder = JsonlRecorder(str(tmp_path / "raw.jsonl"))
    suite = BatchFOKWithRawSuiteV187(settings, recorder)
    engine = _engine(settings)

    # Make both books far older than the protected BFOK freshness limit. RAW
    # must still execute because book-age filtering is intentionally disabled.
    engine.books["A"].updated_monotonic = time.monotonic() - 5
    engine.books["B"].updated_monotonic = time.monotonic() - 5
    surge = SimpleNamespace(active=True)

    suite.on_market_update(engine, "raw187", surge=surge)

    raw = next(v for v in suite.variants if v.strategy == "BFOK-RAW")
    protected = next(v for v in suite.variants if v.strategy == "BFOK-5")

    assert raw.placements == 1
    assert raw.both_filled == 1
    assert raw.one_leg_miss == 0
    # 0.55 + 0.55 plus fees is deliberately loss-making; no edge gate means
    # RAW still books it instead of silently filtering it away.
    assert raw.equity.equity < 0
    assert protected.placements == 0

    # No cooldown: the next valid market update is immediately eligible again.
    suite.on_market_update(engine, "raw187", surge=surge)
    assert raw.placements == 2
    assert raw.both_filled == 2

    rows = [json.loads(line) for line in (tmp_path / "raw.jsonl").read_text().splitlines()]
    summaries = [
        row["payload"]
        for row in rows
        if row["event_type"] == "batch_fok_execution_summary_v187"
        and row["payload"].get("strategy") == "BFOK-RAW"
    ]
    assert len(summaries) == 2
    assert all(p["mode"] == "BATCH_FOK_RAW_V187" for p in summaries)
    assert all(p["raw_ungated"] is True for p in summaries)
    assert all(p["zero_latency_upper_bound"] is True for p in summaries)
    assert all(p["configured_batch_arrival_latency_ms"] == 0 for p in summaries)
    assert all(Decimal(str(p["realized_pnl"])) < 0 for p in summaries)


def test_raw_is_additive_to_standard_bfok_family(tmp_path):
    settings = _settings(
        v187_batch_sizes=(Decimal("1"), Decimal("5"), Decimal("10"), Decimal("20")),
        v187_ev_enabled=True,
    )
    recorder = JsonlRecorder(str(tmp_path / "names.jsonl"))
    suite = BatchFOKWithRawSuiteV187(settings, recorder)
    names = [v.strategy for v in suite.variants]
    assert names == ["BFOK-1", "BFOK-5", "BFOK-10", "BFOK-20", "BFOK-EV", "BFOK-RAW"]
