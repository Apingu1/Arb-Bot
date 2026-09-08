from __future__ import annotations

import json
from decimal import Decimal

import arb_bot.maker_research as maker_base
from arb_bot.config_v181 import SettingsV181
from arb_bot.maker_research import MarketRegimeTracker, SurgeSnapshot
from arb_bot.models import MarketPair
from arb_bot.report_v182 import build_compact_v182
from arb_bot.selective_research_v182 import SelectivePairedMakerVariantV182
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


def test_v182_selective_records_first_fill_and_post_fill_timing(tmp_path, monkeypatch):
    monkeypatch.setattr(maker_base, "market_phase", lambda pair: maker_base.MarketPhase.LIVE)
    settings = SettingsV181(
        maker_min_seconds_to_expiry=0,
        maker_use_empirical_risk_gate=False,
    )
    path = tmp_path / "telemetry.jsonl"
    variant = SelectivePairedMakerVariantV182(
        settings,
        JsonlRecorder(str(path)),
        MarketRegimeTracker(settings),
        max_pair=Decimal("0.97"),
        max_queue=Decimal("10"),
    )
    engine = _engine(settings)

    variant.on_market_update(engine, "m-live", SurgeSnapshot(False, ()))
    engine.books["A"].apply_trade("0.40", "7", "SELL")
    variant.on_market_update(engine, "m-live", SurgeSnapshot(False, ()))

    campaign = variant.campaigns["m-live"]
    assert campaign.first_fill_at is not None
    campaign.first_fill_at -= 0.30
    variant._sample_post_fill_edge(engine, _pair(), campaign)

    events = [json.loads(line) for line in path.read_text().splitlines()]
    timing = next(
        row["payload"]
        for row in events
        if row["event_type"] == "maker_variant_first_fill_timing_v182"
    )
    assert timing["strategy"] == "SPMAKER-P97-Q10"
    assert "complete_now_net_edge_per_share" in timing
    assert "edge_deterioration_vs_quoted_gross" in timing

    checkpoints = {
        int(row["payload"]["target_elapsed_ms"])
        for row in events
        if row["event_type"] == "maker_variant_post_fill_edge_sample"
    }
    assert {25, 50, 100, 250}.issubset(checkpoints)


def test_v182_report_collects_edge_and_atomic_timing(tmp_path):
    path = tmp_path / "report.jsonl"
    rows = [
        {
            "event_type": "maker_variant_execution_summary",
            "payload": {
                "strategy": "SHYB-97-I2-Q25",
                "slug": "doge-updown-15m-1",
                "status": "MAKER_PLUS_TAKER_COMPLETED",
                "realized_pnl": "0.01",
                "market_episode_id": "doge-updown-15m-1:EP:1",
                "first_fill_to_finalize_ms": "44",
                "first_fill_snapshot": {
                    "queue_imbalance_initial": "5",
                    "first_fill_ms": "188",
                    "complete_now_net_edge_per_share": "-0.006",
                    "small_queue_initial": "5",
                    "max_queue_initial": "25",
                },
                "post_fill_edge_timeline": {
                    "25": {
                        "target_elapsed_ms": "25",
                        "actual_elapsed_ms": "27",
                        "complete_now_net_edge_per_share": "-0.004",
                    }
                },
            },
        },
        {
            "event_type": "atomic_benchmark_capture",
            "payload": {
                "strategy": "ATOMIC-BUY-S5",
                "slug": "hype-updown-15m-1",
                "asset": "HYPE",
                "realized_pnl": "0.15",
                "phase182_latency_instrumented": True,
            },
        },
        {
            "event_type": "atomic_benchmark_lifetime",
            "payload": {
                "strategy": "ATOMIC-BUY-S5",
                "slug": "hype-updown-15m-1",
                "asset": "HYPE",
                "lifetime_ms": "8.4",
            },
        },
        {
            "event_type": "atomic_benchmark_latency_sample",
            "payload": {
                "strategy": "ATOMIC-BUY-S5",
                "slug": "hype-updown-15m-1",
                "asset": "HYPE",
                "target_latency_ms": 5,
                "actual_elapsed_ms": "5.3",
                "edge_per_share": "0.02",
                "pnl": "0.10",
            },
        },
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    result = build_compact_v182(path)
    assert result["stats"]["SHYB-97-I2-Q25"]["true_wins"] == 1
    record = result["selective_records"][0]
    assert record["edge_at_fill"] == Decimal("-0.006")
    assert record["fill_to_finalize_ms"] == Decimal("44")

    atomic = result["atomic"]["ATOMIC-BUY-S5"]
    assert atomic["instrumented_captures"] == 1
    assert atomic["lifetimes"] == [Decimal("8.4")]
    assert atomic["latency_samples"][5][0]["edge"] == Decimal("0.02")
