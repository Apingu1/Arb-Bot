from __future__ import annotations

import json
import time
from decimal import Decimal

import arb_bot.maker_research as maker_base
from arb_bot.atomic_benchmark import AtomicWindow
from arb_bot.atomic_benchmark_v183 import IdealAtomicVariantV183
from arb_bot.config_v181 import SettingsV181
from arb_bot.maker_research import MarketRegimeTracker, SurgeSnapshot
from arb_bot.models import MarketPair
from arb_bot.report_v182 import build_compact_v182
from arb_bot.report_v183 import (
    _atomic_outcome_table,
    _corrected_trajectory_section,
    _ghost_gate_table,
    _latest_run_id,
)
from arb_bot.selective_research_v183 import SelectivePairedMakerVariantV183
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


def test_v183_records_first_fill_action_counterfactual_and_ghost_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(maker_base, "market_phase", lambda pair: maker_base.MarketPhase.LIVE)
    settings = SettingsV181(
        maker_min_seconds_to_expiry=0,
        maker_use_empirical_risk_gate=False,
    )
    path = tmp_path / "telemetry.jsonl"
    variant = SelectivePairedMakerVariantV183(
        settings,
        JsonlRecorder(str(path)),
        MarketRegimeTracker(settings),
        max_pair=Decimal("0.97"),
        max_queue=Decimal("10"),
    )
    engine = _engine(settings)

    variant.on_market_update(engine, "m-live", SurgeSnapshot(False, ()))
    campaign = variant.campaigns["m-live"]

    # Make A-first completion economics deteriorate while both maker orders are
    # still resting, then observe the ghost cancellation gate without changing
    # the real strategy.
    engine.books["B"].apply_snapshot(
        [{"price": "0.57", "size": "6"}],
        [{"price": "0.63", "size": "20"}],
    )
    variant._observe_ghost_prefill_gates(engine, _pair(), campaign)
    assert variant._ghost_prefill_gates["m-live"]["A"]

    # Give the ghost gate enough artificial lead time to exercise the 5/10/25ms
    # cancellation counterfactual deterministically.
    for trigger in variant._ghost_prefill_gates["m-live"]["A"].values():
        trigger["triggered_at_monotonic"] -= 0.100

    engine.books["A"].apply_trade("0.40", "7", "SELL")
    variant.on_market_update(engine, "m-live", SurgeSnapshot(False, ()))
    campaign = variant.campaigns["m-live"]
    assert campaign.first_fill_at is not None

    # Finalize directly so this unit test isolates the observational wrapper.
    variant._finalize(
        campaign,
        Decimal("-0.020"),
        status="PARTIAL_OR_ONE_SIDED_EXIT",
        action="UNWIND_RESIDUAL_INVENTORY",
        extra={"reason": "TEST"},
    )

    rows = _events(path)
    choices = next(
        row["payload"]
        for row in rows
        if row["event_type"] == "maker_variant_first_fill_choices_v183"
    )
    assert choices["complete_now_total_pnl"] is not None
    assert choices["unwind_now_total_pnl"] is not None

    result = next(
        row["payload"]
        for row in rows
        if row["event_type"] == "maker_variant_first_fill_counterfactual_v183"
    )
    assert result["actual_eventual_pnl"] == "-0.020"
    assert result["best_action"] in {"COMPLETE_NOW", "UNWIND_NOW", "ACTUAL_POLICY"}
    assert "actual_policy_regret" in result

    ghost = next(
        row["payload"]
        for row in rows
        if row["event_type"] == "maker_variant_ghost_prefill_gate_result_v183"
    )
    any_side = ghost["results"]["-0.005"]["ANY_SIDE"]
    assert any_side["triggered"] is True
    assert any_side["latencies"]["25"]["would_avoid_first_fill"] is True
    assert any_side["latencies"]["25"]["counterfactual_pnl"] == "0"


def test_v183_tracks_second_resting_maker_fill(tmp_path, monkeypatch):
    monkeypatch.setattr(maker_base, "market_phase", lambda pair: maker_base.MarketPhase.LIVE)
    settings = SettingsV181(
        maker_min_seconds_to_expiry=0,
        maker_use_empirical_risk_gate=False,
    )
    path = tmp_path / "second.jsonl"
    variant = SelectivePairedMakerVariantV183(
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
    assert "m-live" in variant.campaigns

    engine.books["B"].apply_trade("0.57", "7", "SELL")
    variant.on_market_update(engine, "m-live", SurgeSnapshot(False, ()))

    rows = _events(path)
    second = [
        row["payload"]
        for row in rows
        if row["event_type"] == "maker_variant_second_maker_fill_v183"
    ]
    assert len(second) == 1
    assert Decimal(second[0]["second_maker_fill_qty"]) > 0
    assert Decimal(second[0]["time_to_second_maker_fill_ms"]) >= 0


def test_v183_atomic_distinguishes_scheduler_miss_from_expiry(tmp_path):
    settings = SettingsV181()
    path = tmp_path / "atomic.jsonl"
    variant = IdealAtomicVariantV183(
        settings,
        JsonlRecorder(str(path)),
        shares=Decimal("1"),
        direction="BUY_PAIR",
    )
    started = time.monotonic()
    variant.active["m-live"] = AtomicWindow(
        slug="doge-updown-15m-1999999800",
        started_at=started,
        started_at_utc="2026-09-08T15:00:00Z",
        capture_edge=Decimal("0.02"),
        peak_edge=Decimal("0.02"),
        capture_pnl=Decimal("0.02"),
        pair_price=Decimal("0.95"),
    )
    variant._sampled_checkpoints["m-live"] = set()
    variant._close("m-live", started + 0.003, "TEST_CLOSE")

    outcomes = {
        int(row["payload"]["target_latency_ms"]): row["payload"]["outcome"]
        for row in _events(path)
        if row["event_type"] == "atomic_benchmark_latency_outcome_v183"
    }
    assert outcomes[1] == "SCHEDULER_MISSED_CHECKPOINT"
    assert outcomes[2] == "SCHEDULER_MISSED_CHECKPOINT"
    assert outcomes[5] == "EXPIRED_BEFORE_CHECKPOINT"
    assert outcomes[10] == "EXPIRED_BEFORE_CHECKPOINT"


def test_v183_report_uses_instrumented_denominator_and_session_events(tmp_path):
    path = tmp_path / "report.jsonl"
    rows = [
        {
            "event_type": "maker_variant_execution_summary",
            "payload": {
                "phase183_instrumented": True,
                "phase183_run_id": "run-b",
                "strategy": "SHYB-97-I2-Q25",
                "slug": "doge-updown-15m-1",
                "status": "PARTIAL_OR_ONE_SIDED_EXIT",
                "realized_pnl": "-0.02",
                "first_fill_to_finalize_ms": "40",
                "first_fill_snapshot": {
                    "first_fill_ms": "300",
                    "complete_now_net_edge_per_share": "-0.012",
                },
                "post_fill_edge_timeline": {
                    "10": {
                        "actual_elapsed_ms": "10.4",
                        "complete_now_net_edge_per_share": "-0.013",
                    }
                },
            },
        },
        {
            "event_type": "maker_variant_execution_summary",
            "payload": {
                "strategy": "SHYB-97-I2-Q25",
                "slug": "doge-updown-15m-old",
                "status": "PARTIAL_OR_ONE_SIDED_EXIT",
                "realized_pnl": "-0.02",
                "first_fill_snapshot": {
                    "first_fill_ms": "500",
                    "complete_now_net_edge_per_share": "-0.02",
                },
            },
        },
        {
            "event_type": "maker_variant_ghost_prefill_gate_result_v183",
            "payload": {
                "phase183_run_id": "run-b",
                "strategy": "SHYB-97-I2-Q25",
                "slug": "doge-updown-15m-1",
                "actual_pnl": "-0.02",
                "status": "PARTIAL_OR_ONE_SIDED_EXIT",
                "results": {
                    "-0.010": {
                        "ANY_SIDE": {
                            "triggered": True,
                            "latencies": {
                                "25": {
                                    "would_avoid_first_fill": True,
                                    "counterfactual_pnl": "0",
                                    "delta_vs_actual": "0.02",
                                }
                            },
                        }
                    }
                },
            },
        },
        {
            "event_type": "atomic_benchmark_latency_outcome_v183",
            "payload": {
                "phase183_run_id": "run-b",
                "strategy": "ATOMIC-BUY-S1",
                "slug": "doge-updown-15m-1",
                "target_latency_ms": 1,
                "outcome": "SURVIVED",
                "actual_elapsed_ms": "1.1",
                "edge_per_share": "0.02",
                "pnl": "0.02",
            },
        },
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    result = build_compact_v182(path)
    trajectory = _corrected_trajectory_section(result, "SHYB-97-I2-Q25", None)
    assert "denominator=1 instrumented" in trajectory
    assert "10ms" in trajectory
    assert "100.0%" in trajectory

    assert _latest_run_id(path) == "run-b"
    ghost = _ghost_gate_table(
        path,
        run_id="run-b",
        strategy="SHYB-97-I2-Q25",
        asset=None,
    )
    assert "ANY_SIDE" in ghost
    assert "+0.0200" in ghost

    atomic = _atomic_outcome_table(
        path,
        run_id="run-b",
        strategy=None,
        asset=None,
    )
    assert "ATOMIC-BUY-S1" in atomic
    assert "100.0%" in atomic
