from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import arb_bot.atomic_benchmark as atomic_mod
import arb_bot.maker_research as maker_base
import arb_bot.maker_research_v17 as maker_v17
from arb_bot.atomic_benchmark import IdealAtomicBenchmarkSuite
from arb_bot.config import Settings
from arb_bot.dashboard_v17 import DashboardStateV17, ENHANCED_HTML
from arb_bot.dual_fok_v17 import DualFOKResearchSuiteV17
from arb_bot.maker_research import SurgeSnapshot
from arb_bot.maker_research_v17 import MakerResearchSuiteV17, MultiAssetQueueAwareVariantEngine
from arb_bot.models import MarketPair
from arb_bot.report_v17 import build_report
from arb_bot.storage import JsonlRecorder
from arb_bot.strategy import ArbitrageEngine


def _pair(slug: str = "btc-updown-15m-1999999800") -> MarketPair:
    return MarketPair(
        market_id="m-live",
        condition_id="c-live",
        slug=slug,
        question="Crypto Up or Down 15m",
        outcome_a="Up",
        outcome_b="Down",
        token_a="A",
        token_b="B",
    )


def _engine(settings: Settings, *, bid_a="0.44", ask_a="0.45", bid_b="0.44", ask_b="0.45", size_a="100", size_b="100"):
    engine = ArbitrageEngine(settings)
    engine.set_markets([_pair()])
    engine.books["A"].apply_snapshot([{"price": bid_a, "size": size_a}], [{"price": ask_a, "size": size_a}])
    engine.books["B"].apply_snapshot([{"price": bid_b, "size": size_b}], [{"price": ask_b, "size": size_b}])
    return engine


def test_ideal_atomic_captures_positive_snapshot_without_strategy_equity(tmp_path, monkeypatch):
    monkeypatch.setattr(atomic_mod, "market_phase", lambda pair: atomic_mod.MarketPhase.LIVE)
    settings = Settings(
        atomic_benchmark_enabled=True,
        atomic_reverse_enabled=False,
        atomic_sizes=(Decimal("1"),),
        atomic_min_net_edge_per_share=Decimal("0.0001"),
        atomic_edge_bands=(Decimal("0.001"), Decimal("0.005")),
        atomic_max_book_age_ms=30000,
    )
    path = tmp_path / "atomic.jsonl"
    suite = IdealAtomicBenchmarkSuite(settings, JsonlRecorder(str(path)))
    engine = _engine(settings)

    suite.on_market_update(engine, "m-live")
    row = suite.diagnostic_rows()[0]
    assert row["captures"] == 1
    assert row["benchmark_pnl"] > 0

    event_types = [json.loads(line)["event_type"] for line in path.read_text().splitlines()]
    assert "atomic_benchmark_capture" in event_types
    assert "strategy_equity" not in event_types

    # Remove the edge to close the contiguous atomic opportunity and measure life.
    engine.books["B"].apply_snapshot([{"price": "0.40", "size": "100"}], [{"price": "0.70", "size": "100"}])
    suite.on_market_update(engine, "m-live")
    assert suite.diagnostic_rows()[0]["lifetime_samples"] == 1


def test_paired_maker_only_joins_existing_low_balanced_queues(tmp_path, monkeypatch):
    monkeypatch.setattr(maker_base, "market_phase", lambda pair: maker_base.MarketPhase.LIVE)
    settings = Settings(
        maker_enabled=True,
        hybrid_enabled=False,
        paired_maker_enabled=True,
        paired_maker_trade_shares=Decimal("1"),
        paired_maker_target_pair=Decimal("0.99"),
        paired_maker_min_gross_edge_per_share=Decimal("0.005"),
        paired_maker_max_queues=(Decimal("25"),),
        paired_maker_max_queue_imbalance=Decimal("4"),
        maker_min_seconds_to_expiry=0,
    )
    suite = MakerResearchSuiteV17(settings, JsonlRecorder(str(tmp_path / "pmaker.jsonl")))
    engine = _engine(settings, bid_a="0.49", ask_a="0.51", bid_b="0.49", ask_b="0.51", size_a="10", size_b="10")
    suite.on_market_update(engine, "m-live")
    paired = suite.paired_makers[0]
    assert paired.placed == 1
    campaign = paired.campaigns["m-live"]
    assert campaign.leg_a.price == Decimal("0.49")
    assert campaign.leg_b.price == Decimal("0.49")

    # A queue above the cap must be rejected rather than quoted anyway.
    suite2 = MakerResearchSuiteV17(settings, JsonlRecorder(str(tmp_path / "pmaker2.jsonl")))
    engine2 = _engine(settings, bid_a="0.49", ask_a="0.51", bid_b="0.49", ask_b="0.51", size_a="30", size_b="10")
    suite2.on_market_update(engine2, "m-live")
    paired2 = suite2.paired_makers[0]
    assert paired2.placed == 0
    assert paired2.queue_skips >= 1


def test_multi_asset_maker_expiry_uses_generic_window(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(maker_v17, "updown_15m_window_from_slug", lambda slug: (now, now + timedelta(seconds=12)))
    settings = Settings()
    engine = MultiAssetQueueAwareVariantEngine(
        settings,
        JsonlRecorder(str(tmp_path / "expiry.jsonl")),
        maker_base.MarketRegimeTracker(settings),
        mode="MAKER",
        target_pair=Decimal("0.99"),
    )
    seconds = engine._seconds_to_expiry("eth-updown-15m-1999999800")
    assert 0 <= seconds <= 12.1


def test_dashboard_separates_session_from_all_time_and_attributes_asset(tmp_path):
    settings = Settings(
        market_assets=("BTC", "ETH"),
        dashboard_state_path=str(tmp_path / "dashboard.json"),
        dashboard_event_limit=20,
    )
    state = DashboardStateV17(settings)
    historical = {
        "recorded_at": "2026-09-08T00:00:00Z",
        "strategy": "TAKER",
        "slug": "btc-updown-15m-1999999800",
        "status": "ONE_LEG_MISS",
        "action": "UNWIND",
        "pnl_delta": "-1.00",
    }
    live = dict(historical, recorded_at="2026-09-08T00:01:00Z", status="BOTH_FILLED", action="MERGE", pnl_delta="0.20")
    state._ingest("strategy_equity", historical, historical=True)
    state._ingest("strategy_equity", live, historical=False)

    engine = SimpleNamespace(pairs={}, books={})
    taker = SimpleNamespace(
        pending_count=0,
        completed=0,
        leg_misses=0,
        rejected=0,
        total_pnl=Decimal("0"),
        empirical_risk=SimpleNamespace(attempts=0, miss_probability=Decimal("0")),
    )
    diagnostics = SimpleNamespace(total_messages=0)
    published = state.publish(engine, taker, None, diagnostics)

    assert published["session_shadow_pnl"] == 0.2
    assert published["all_time_shadow_pnl"] == -0.8
    assert published["aggregate_shadow_pnl"] == 0.2
    btc_taker = next(row for row in published["asset_strategies"] if row["asset"] == "BTC" and row["strategy"] == "TAKER")
    assert btc_taker["session_pnl"] == 0.2
    assert btc_taker["all_time_pnl"] == -0.8
    assert "All-Time Research P&L" in ENHANCED_HTML
    assert "ASSET × MODEL ATTRIBUTION" in ENHANCED_HTML


def test_v17_dual_fok_names_use_plain_decimal_sizes(tmp_path):
    settings = Settings(
        dual_fok_enabled=True,
        reverse_dual_fok_enabled=False,
        dual_fok_primary_size=Decimal("1"),
        dual_fok_primary_edge_target=Decimal("0.001"),
        dual_fok_primary_skew_ms=2,
        dual_fok_primary_coverage_multiple=Decimal("2"),
        dual_fok_primary_stability_ms=0,
        dual_fok_skews_ms=(0, 2),
        dual_fok_size_candidates=(Decimal("1"), Decimal("10"), Decimal("20")),
        dual_fok_edge_targets=(Decimal("0.001"),),
        dual_fok_coverage_multiples=(Decimal("2"),),
        dual_fok_stability_periods_ms=(0,),
    )
    suite = DualFOKResearchSuiteV17(settings, JsonlRecorder(str(tmp_path / "dfok.jsonl")))
    names = [variant.spec.strategy for variant in suite.variants]
    assert any("-S10-" in name for name in names)
    assert any("-S20-" in name for name in names)
    assert all("E+" not in name for name in names)


def test_v17_report_recognizes_atomic_capture(tmp_path):
    path = tmp_path / "report.jsonl"
    path.write_text(
        json.dumps(
            {
                "event_type": "atomic_benchmark_capture",
                "payload": {
                    "strategy": "ATOMIC-BUY-S1",
                    "mode": "IDEAL_ATOMIC_BENCHMARK",
                    "direction": "BUY_PAIR",
                    "status": "CAPTURED",
                    "action": "INSTANT_SIMULTANEOUS_COMPLETE_SET",
                    "shares": "1",
                    "slug": "btc-updown-15m-1999999800",
                    "detected_pair_price": "0.99",
                    "detected_edge_per_share": "0.002",
                    "realized_pnl": "0.002",
                    "equity_after": "0.002",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    summary, executions = build_report(path)
    assert summary["ATOMIC-BUY-S1"]["wins"] == 1
    assert summary["ATOMIC-BUY-S1"]["pnl"] == Decimal("0.002")
    assert executions[0]["mode"] == "IDEAL_ATOMIC_BENCHMARK"
