from __future__ import annotations

import json
from decimal import Decimal

import arb_bot.maker_research as maker_base
from arb_bot.config_v181 import SettingsV181
from arb_bot.episode_clustering import OutcomeEpisodeClusterer
from arb_bot.maker_research import MarketRegimeTracker, SurgeSnapshot
from arb_bot.models import MarketPair
from arb_bot.report_v181 import build_compact
from arb_bot.selective_research_v181 import (
    SelectiveHybridVariant,
    SelectiveMakerVariant,
    SelectivePairedMakerVariant,
)
from arb_bot.storage import JsonlRecorder
from arb_bot.strategy import ArbitrageEngine
from arb_bot.winner_research_v181 import WinnerResearchSuiteV181


def _pair(slug: str = "eth-updown-15m-1999999800") -> MarketPair:
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


def _engine(
    settings,
    *,
    bid_a="0.32",
    ask_a="0.33",
    bid_b="0.65",
    ask_b="0.66",
    size_a="5",
    size_b="20",
):
    engine = ArbitrageEngine(settings)
    engine.set_markets([_pair()])
    engine.books["A"].apply_snapshot(
        [{"price": bid_a, "size": size_a}],
        [{"price": ask_a, "size": size_a}],
    )
    engine.books["B"].apply_snapshot(
        [{"price": bid_b, "size": size_b}],
        [{"price": ask_b, "size": size_b}],
    )
    return engine


def test_selective_hybrid_requires_asymmetric_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(maker_base, "market_phase", lambda pair: maker_base.MarketPhase.LIVE)
    settings = SettingsV181(
        maker_min_seconds_to_expiry=0,
        maker_use_empirical_risk_gate=False,
    )
    recorder = JsonlRecorder(str(tmp_path / "hybrid.jsonl"))
    variant = SelectiveHybridVariant(
        settings,
        recorder,
        MarketRegimeTracker(settings),
        min_imbalance=Decimal("2"),
    )

    engine = _engine(settings, size_a="5", size_b="20")
    variant.on_market_update(engine, "m-live", SurgeSnapshot(False, ()))
    assert variant.placed == 1
    assert "m-live" in variant.campaigns

    recorder2 = JsonlRecorder(str(tmp_path / "hybrid-balanced.jsonl"))
    variant2 = SelectiveHybridVariant(
        settings,
        recorder2,
        MarketRegimeTracker(settings),
        min_imbalance=Decimal("2"),
    )
    balanced = _engine(settings, size_a="10", size_b="10")
    variant2.on_market_update(balanced, "m-live", SurgeSnapshot(False, ()))
    assert variant2.placed == 0
    assert variant2.selection_imbalance_skips >= 1


def test_selective_pmaker_requires_deep_pair_and_tiny_queues(tmp_path, monkeypatch):
    monkeypatch.setattr(maker_base, "market_phase", lambda pair: maker_base.MarketPhase.LIVE)
    settings = SettingsV181(
        maker_min_seconds_to_expiry=0,
        maker_use_empirical_risk_gate=False,
    )
    variant = SelectivePairedMakerVariant(
        settings,
        JsonlRecorder(str(tmp_path / "spmaker.jsonl")),
        MarketRegimeTracker(settings),
        max_pair=Decimal("0.97"),
        max_queue=Decimal("10"),
    )
    engine = _engine(
        settings,
        bid_a="0.40",
        ask_a="0.41",
        bid_b="0.57",
        ask_b="0.58",
        size_a="6",
        size_b="7",
    )
    variant.on_market_update(engine, "m-live", SurgeSnapshot(False, ()))
    assert variant.placed == 1

    variant2 = SelectivePairedMakerVariant(
        settings,
        JsonlRecorder(str(tmp_path / "spmaker-bad.jsonl")),
        MarketRegimeTracker(settings),
        max_pair=Decimal("0.97"),
        max_queue=Decimal("10"),
    )
    bad_pair = _engine(
        settings,
        bid_a="0.40",
        ask_a="0.41",
        bid_b="0.58",
        ask_b="0.59",
        size_a="6",
        size_b="7",
    )
    variant2.on_market_update(bad_pair, "m-live", SurgeSnapshot(False, ()))
    assert variant2.placed == 0
    assert variant2.selection_pair_skips >= 1


def test_selective_maker_97_q10_places_on_small_balanced_target_queues(tmp_path, monkeypatch):
    monkeypatch.setattr(maker_base, "market_phase", lambda pair: maker_base.MarketPhase.LIVE)
    settings = SettingsV181(
        maker_min_seconds_to_expiry=0,
        maker_use_empirical_risk_gate=False,
    )
    variant = SelectiveMakerVariant(
        settings,
        JsonlRecorder(str(tmp_path / "smaker.jsonl")),
        MarketRegimeTracker(settings),
        target_pair=Decimal("0.97"),
        max_queue=Decimal("10"),
    )
    engine = _engine(
        settings,
        bid_a="0.40",
        ask_a="0.41",
        bid_b="0.57",
        ask_b="0.58",
        size_a="6",
        size_b="6",
    )
    variant.on_market_update(engine, "m-live", SurgeSnapshot(False, ()))
    assert variant.placed == 1
    assert variant.shares == Decimal("1")


def test_first_fill_snapshot_records_completion_context(tmp_path, monkeypatch):
    monkeypatch.setattr(maker_base, "market_phase", lambda pair: maker_base.MarketPhase.LIVE)
    settings = SettingsV181(
        maker_min_seconds_to_expiry=0,
        maker_use_empirical_risk_gate=False,
    )
    path = tmp_path / "telemetry.jsonl"
    variant = SelectivePairedMakerVariant(
        settings,
        JsonlRecorder(str(path)),
        MarketRegimeTracker(settings),
        max_pair=Decimal("0.97"),
        max_queue=Decimal("10"),
    )
    engine = _engine(
        settings,
        bid_a="0.40",
        ask_a="0.41",
        bid_b="0.57",
        ask_b="0.58",
        size_a="6",
        size_b="6",
    )
    variant.on_market_update(engine, "m-live", SurgeSnapshot(False, ()))
    engine.books["A"].apply_trade("0.40", "7", "SELL")
    variant.on_market_update(engine, "m-live", SurgeSnapshot(False, ()))

    events = [json.loads(line) for line in path.read_text().splitlines()]
    snapshot = next(
        row["payload"]
        for row in events
        if row["event_type"] == "maker_variant_first_fill_snapshot"
    )
    assert snapshot["strategy"] == "SPMAKER-P97-Q10"
    assert snapshot["first_fill_side"] == "A"
    assert snapshot["queue_imbalance_initial"] == "1"
    assert "complete_now_net_edge_per_share" in snapshot
    assert "midpoint_move_250ms" in snapshot


def test_episode_clusterer_groups_correlated_model_outcomes():
    clusterer = OutcomeEpisodeClusterer()
    first = clusterer.assign("bnb-updown-15m-1", window_ms=2000)
    second = clusterer.assign("bnb-updown-15m-1", window_ms=2000)
    other = clusterer.assign("sol-updown-15m-1", window_ms=2000)
    assert first == second
    assert other != first


def test_v181_suite_contains_selective_variants(tmp_path):
    settings = SettingsV181()
    suite = WinnerResearchSuiteV181(
        settings,
        JsonlRecorder(str(tmp_path / "suite.jsonl")),
    )
    names = {variant.strategy_name for variant in suite.selective_variants}
    assert names == {
        "SHYB-97-I2",
        "SHYB-97-I3",
        "SHYB-97-I4",
        "SHYB-97-I2-Q25",
        "SPMAKER-P95-Q10",
        "SPMAKER-P97-Q10",
        "SPMAKER-P97-Q15",
        "SPMAKER-P97-Q25",
        "SMAKER-97-Q10",
    }


def test_report_counts_correlated_episode_once_globally(tmp_path):
    path = tmp_path / "report.jsonl"
    rows = []
    for strategy in ("SPMAKER-P97-Q10", "SMAKER-97-Q10"):
        rows.append(
            {
                "event_type": "maker_variant_execution_summary",
                "payload": {
                    "strategy": strategy,
                    "slug": "bnb-updown-15m-1",
                    "status": "BOTH_MAKER_FILLED",
                    "realized_pnl": "0.03",
                    "market_episode_id": "bnb-updown-15m-1:EP:test",
                    "first_fill_snapshot": {
                        "queue_imbalance_initial": "1",
                        "first_fill_ms": "50",
                        "complete_now_net_edge_per_share": "0.01",
                        "small_queue_initial": "6",
                        "max_queue_initial": "6",
                    },
                },
            }
        )
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    result = build_compact(path)
    assert result["global_true_win_model_events"] == 2
    assert len(result["global_episode_ids"]) == 1
    assert result["stats"]["SPMAKER-P97-Q10"]["true_wins"] == 1
