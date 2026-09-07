import json
from decimal import Decimal

import arb_bot.ev_frontier as ev_base
from arb_bot.config import Settings
from arb_bot.discovery import MarketPhase
from arb_bot.ev_frontier import FrontierHedgeVariant
from arb_bot.ev_frontier_v151 import CorrectedEVFrontierSuite, CorrectedFrontierHedgeVariant
from arb_bot.maker_research import MarketRegimeTracker, SurgeSnapshot
from arb_bot.models import MarketPair
from arb_bot.report import build_report
from arb_bot.storage import JsonlRecorder
from arb_bot.strategy import ArbitrageEngine


def _settings(**overrides):
    base = dict(
        max_trade_shares=Decimal("50"),
        maker_tick_size=Decimal("0.01"),
        hedge_enabled=True,
        hedge_latency_reserve_per_share=Decimal("0.002"),
        hedge_min_expected_profit_usdc=Decimal("0.10"),
        ev_min_expected_profit_usdc=Decimal("0.01"),
        hedge_max_improve_ticks=0,
        hedge_max_quote_age_ms=30000,
        hedge_requote_cooldown_ms=0,
        hedge_min_seconds_to_expiry=0,
        ev_frontier_enabled=True,
        ev_grace_edge_target=Decimal("0.005"),
        ev_grace_latency_ms=100,
        ev_grace_periods_ms=(0, 100, 250, 500),
        ev_grace_trade_shares=Decimal("5"),
        ev_hard_loss_per_share=Decimal("0.005"),
        ev_size_edge_target=Decimal("0.005"),
        ev_size_latency_ms=100,
        ev_size_grace_ms=250,
        ev_size_candidates=(Decimal("5"), Decimal("10"), Decimal("20"), Decimal("50")),
        surge_move_1s=Decimal("1"),
        surge_move_3s=Decimal("1"),
        surge_updates_per_second=100000,
        split_sell_enabled=False,
    )
    base.update(overrides)
    return Settings(**base)


def _pair():
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


def _engine(settings):
    engine = ArbitrageEngine(settings)
    engine.set_markets([_pair()])
    # This book deliberately produces about $0.05 expected profit on a 5-share
    # E05 quote after tick rounding: below the old $0.10 HEDGE floor but above
    # the new $0.01 EV research floor.
    engine.books["A"].apply_snapshot(
        [{"price": "0.48", "size": "100"}],
        [{"price": "0.49", "size": "100"}],
    )
    engine.books["B"].apply_snapshot(
        [{"price": "0.50", "size": "100"}],
        [{"price": "0.51", "size": "100"}],
    )
    return engine


def test_five_share_ev_variant_uses_ev_profit_floor_not_hedge_floor(tmp_path, monkeypatch):
    monkeypatch.setattr(ev_base, "market_phase", lambda pair: MarketPhase.LIVE)
    settings = _settings()
    engine = _engine(settings)
    regime = MarketRegimeTracker(settings)

    old = FrontierHedgeVariant(
        settings,
        JsonlRecorder(str(tmp_path / "old.jsonl")),
        regime,
        edge_target=Decimal("0.005"),
        completion_latency_ms=100,
        grace_ms=250,
        fixed_size=Decimal("5"),
        strategy_name="OLD-S5",
    )
    corrected = CorrectedFrontierHedgeVariant(
        settings,
        JsonlRecorder(str(tmp_path / "new.jsonl")),
        regime,
        edge_target=Decimal("0.005"),
        completion_latency_ms=100,
        grace_ms=250,
        fixed_size=Decimal("5"),
        strategy_name="NEW-S5",
    )

    old.on_market_update(engine, "m-live", SurgeSnapshot(False, ()))
    corrected.on_market_update(engine, "m-live", SurgeSnapshot(False, ()))

    assert old.placed == 0
    assert corrected.placed == 1
    assert corrected.campaigns["m-live"].placement_expected_profit < Decimal("0.10")
    assert corrected.campaigns["m-live"].placement_expected_profit >= Decimal("0.01")


def test_zero_evidence_variants_are_not_ranked_as_top(tmp_path):
    settings = _settings()
    suite = CorrectedEVFrontierSuite(
        settings,
        JsonlRecorder(str(tmp_path / "suite.jsonl")),
        MarketRegimeTracker(settings),
    )

    assert suite.ranked_rows() == []
    rows = suite.diagnostic_rows()
    assert rows
    assert all(row["sample_status"] == "INSUFFICIENT_DATA" for row in rows)
    assert all("INSUFFICIENT_DATA" in row["strategy"] for row in rows)


def test_report_summarizes_ghost_pnl_distribution_and_reason(tmp_path):
    path = tmp_path / "ghosts.jsonl"
    rows = [
        {
            "event_type": "hedge_campaign_placed",
            "payload": {"strategy": "EV-S5", "market_id": "m1"},
        },
        {
            "event_type": "hedge_campaign_cancelled",
            "payload": {"strategy": "EV-S5", "reason": "SURGE"},
        },
        {
            "event_type": "hedge_ghost_outcome",
            "payload": {
                "strategy": "EV-S5",
                "outcome": "WOULD_FILL",
                "cancel_reason": "SURGE",
                "fill_qty": "5",
                "best_recovery_pnl": "-0.10",
                "best_recovery_pnl_per_share": "-0.02",
                "elapsed_ms": "1200",
                "would_be_profitable": False,
                "would_clear_original_target": False,
            },
        },
        {
            "event_type": "hedge_ghost_outcome",
            "payload": {
                "strategy": "EV-S5",
                "outcome": "WOULD_FILL",
                "cancel_reason": "SURGE",
                "fill_qty": "5",
                "best_recovery_pnl": "-0.20",
                "best_recovery_pnl_per_share": "-0.04",
                "elapsed_ms": "1800",
                "would_be_profitable": False,
                "would_clear_original_target": False,
            },
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    summary, executions = build_report(path)
    item = summary["EV-S5"]

    assert executions == []
    assert item["sample_status"] == "GHOST_ONLY"
    assert item["ghost_avg_best_pnl"] == Decimal("-0.15")
    assert item["ghost_median_best_pnl"] == Decimal("-0.15")
    assert item["ghost_best_pnl"] == Decimal("-0.10")
    assert item["ghost_worst_pnl"] == Decimal("-0.20")
    assert item["ghost_avg_pnl_per_share"] == Decimal("-0.03")
    assert item["ghost_avg_elapsed_ms"] == Decimal("1500")
    assert item["ghost_by_reason"]["SURGE"]["count"] == 2
    assert item["ghost_by_reason"]["SURGE"]["avg_pnl"] == Decimal("-0.15")


def test_split_sell_is_disabled_by_default_for_corrected_research(monkeypatch):
    monkeypatch.delenv("SPLIT_SELL_ENABLED", raising=False)
    settings = Settings()
    assert settings.split_sell_enabled is False
    assert settings.ev_min_expected_profit_usdc == Decimal("0.01")
