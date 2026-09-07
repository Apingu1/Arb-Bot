import json
from decimal import Decimal

import arb_bot.dual_fok_research as dfok
from arb_bot.config import Settings
from arb_bot.discovery import MarketPhase
from arb_bot.dual_fok_research import DualFOKResearchSuite, DualFOKVariantEngine, DualFOKVariantSpec
from arb_bot.maker_research import SurgeSnapshot
from arb_bot.models import MarketPair
from arb_bot.report import build_report
from arb_bot.storage import JsonlRecorder
from arb_bot.strategy import ArbitrageEngine


def _settings(**overrides):
    base = dict(
        dual_fok_enabled=True,
        reverse_dual_fok_enabled=True,
        dual_fok_base_latency_ms=0,
        dual_fok_recovery_latency_ms=0,
        dual_fok_cooldown_ms=0,
        dual_fok_max_book_age_ms=30000,
        dual_fok_use_surge_gate=False,
        dual_fok_leg_order="fragile_first",
        dual_fok_primary_size=Decimal("5"),
        dual_fok_primary_edge_target=Decimal("0.005"),
        dual_fok_primary_skew_ms=25,
        dual_fok_primary_coverage_multiple=Decimal("2"),
        dual_fok_primary_stability_ms=50,
        dual_fok_skews_ms=(0, 10, 25, 50, 100, 200),
        dual_fok_size_candidates=(Decimal("1"), Decimal("5"), Decimal("10"), Decimal("20")),
        dual_fok_edge_targets=(Decimal("0.005"), Decimal("0.010"), Decimal("0.015"), Decimal("0.020"), Decimal("0.030")),
        dual_fok_coverage_multiples=(Decimal("1"), Decimal("2"), Decimal("5")),
        dual_fok_stability_periods_ms=(0, 25, 50, 100, 250),
        reverse_dual_fok_skews_ms=(0, 25, 50),
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


def _engine(settings, *, ask_a="0.45", ask_b="0.45", bid_a="0.44", bid_b="0.44"):
    engine = ArbitrageEngine(settings)
    engine.set_markets([_pair()])
    engine.books["A"].apply_snapshot(
        [{"price": bid_a, "size": "100"}],
        [{"price": ask_a, "size": "100"}],
    )
    engine.books["B"].apply_snapshot(
        [{"price": bid_b, "size": "100"}],
        [{"price": ask_b, "size": "100"}],
    )
    return engine


def _variant(settings, recorder, *, direction="BUY_PAIR", skew=0, stability=0):
    return DualFOKVariantEngine(
        settings,
        recorder,
        DualFOKVariantSpec(
            strategy=f"TEST-{direction}",
            direction=direction,
            shares=Decimal("5"),
            edge_target=Decimal("0.005"),
            arrival_skew_ms=skew,
            coverage_multiple=Decimal("2"),
            stability_ms=stability,
        ),
    )


def test_dual_fok_both_buy_legs_fill_and_lock_complete_set_profit(tmp_path, monkeypatch):
    monkeypatch.setattr(dfok, "market_phase", lambda pair: MarketPhase.LIVE)
    settings = _settings()
    recorder = JsonlRecorder(str(tmp_path / "buy.jsonl"))
    engine = _engine(settings)
    variant = _variant(settings, recorder)

    variant.on_market_update(engine, "m-live", SurgeSnapshot(False, ()))
    variant.process_due(engine)

    row = variant.diagnostic_row()
    assert row["placements"] == 1
    assert row["both_filled"] == 1
    assert row["one_leg_miss"] == 0
    assert row["equity"] > 0


def test_dual_fok_one_leg_miss_uses_lower_loss_recovery(tmp_path, monkeypatch):
    monkeypatch.setattr(dfok, "market_phase", lambda pair: MarketPhase.LIVE)
    settings = _settings()
    recorder = JsonlRecorder(str(tmp_path / "miss.jsonl"))
    engine = _engine(settings)
    variant = _variant(settings, recorder)

    variant.on_market_update(engine, "m-live", SurgeSnapshot(False, ()))
    # Detection-time B limit was 0.45. Pull that liquidity before the FOKs arrive.
    engine.books["B"].apply_snapshot(
        [{"price": "0.30", "size": "100"}],
        [{"price": "0.70", "size": "100"}],
    )
    variant.process_due(engine)
    variant.process_due(engine)

    row = variant.diagnostic_row()
    assert row["one_leg_miss"] == 1
    assert row["both_filled"] == 0
    assert row["recovery_unwinds"] == 1
    assert row["equity"] < 0


def test_reverse_dual_fok_sells_prepositioned_complete_set_profitably(tmp_path, monkeypatch):
    monkeypatch.setattr(dfok, "market_phase", lambda pair: MarketPhase.LIVE)
    settings = _settings()
    recorder = JsonlRecorder(str(tmp_path / "reverse.jsonl"))
    engine = _engine(settings, ask_a="0.56", ask_b="0.56", bid_a="0.55", bid_b="0.55")
    variant = _variant(settings, recorder, direction="SELL_PAIR")

    variant.on_market_update(engine, "m-live", SurgeSnapshot(False, ()))
    variant.process_due(engine)

    row = variant.diagnostic_row()
    assert row["both_filled"] == 1
    assert row["equity"] > 0

    payloads = [json.loads(line) for line in (tmp_path / "reverse.jsonl").read_text().splitlines()]
    summaries = [r["payload"] for r in payloads if r["event_type"] == "dual_fok_execution_summary"]
    assert summaries[-1]["prepositioned_complete_set_inventory"] is True


def test_quote_stability_and_opportunity_lifetime_are_measured(tmp_path, monkeypatch):
    monkeypatch.setattr(dfok, "market_phase", lambda pair: MarketPhase.LIVE)
    clock = {"now": 100.0}
    monkeypatch.setattr(dfok.time, "monotonic", lambda: clock["now"])
    settings = _settings(dual_fok_max_book_age_ms=1000000)
    recorder = JsonlRecorder(str(tmp_path / "lifetime.jsonl"))
    engine = _engine(settings)
    variant = _variant(settings, recorder, stability=100)

    variant.on_market_update(engine, "m-live", SurgeSnapshot(False, ()))
    assert variant.placements == 0
    clock["now"] = 100.050
    variant.on_market_update(engine, "m-live", SurgeSnapshot(False, ()))
    assert variant.placements == 0

    # Change one detection-time marginal price before the 100ms stability gate.
    clock["now"] = 100.075
    engine.books["B"].apply_snapshot(
        [{"price": "0.43", "size": "100"}],
        [{"price": "0.44", "size": "100"}],
    )
    variant.on_market_update(engine, "m-live", SurgeSnapshot(False, ()))

    assert variant.opportunities_ended == 1
    assert Decimal("70") <= variant.opportunity_lifetimes_ms[0] <= Decimal("80")
    assert variant.placements == 0


def test_suite_builds_focused_families_not_cartesian_grid(tmp_path):
    settings = _settings()
    suite = DualFOKResearchSuite(settings, JsonlRecorder(str(tmp_path / "suite.jsonl")))
    names = [v.spec.strategy for v in suite.variants]

    # 6 skew + 3 non-baseline sizes + 4 non-baseline edges + 2 non-baseline
    # coverage + 4 non-baseline stability + 3 reverse-skew = 22 variants.
    assert len(names) == 22
    assert any(name.startswith("DFOK-SK-S5-E05-SK0") for name in names)
    assert any(name.startswith("DFOK-SZ-S1-E05") for name in names)
    assert any(name.startswith("DFOK-ED-S5-E30") for name in names)
    assert any(name.startswith("RFOK-SK-S5-E05") for name in names)


def test_report_includes_dual_fok_execution_and_lifetime_metrics(tmp_path):
    path = tmp_path / "report.jsonl"
    rows = [
        {"event_type": "dual_fok_opportunity_started", "payload": {"strategy": "DFOK-X"}},
        {"event_type": "dual_fok_attempt_placed", "payload": {"strategy": "DFOK-X"}},
        {"event_type": "dual_fok_opportunity_lifetime", "payload": {"strategy": "DFOK-X", "lifetime_ms": "25"}},
        {
            "event_type": "dual_fok_execution_summary",
            "payload": {
                "strategy": "DFOK-X",
                "mode": "DUAL_FOK",
                "direction": "BUY_PAIR",
                "status": "BOTH_FILLED",
                "action": "MERGE_COMPLETE_SET",
                "shares": "5",
                "target_edge_per_share": "0.005",
                "arrival_skew_ms": 25,
                "base_latency_ms": 25,
                "stability_ms": 50,
                "coverage_multiple": "2",
                "detected_pair_price": "0.95",
                "detected_edge_per_share": "0.01",
                "detected_coverage_multiple": "4",
                "realized_pnl": "0.05",
                "equity_after": "0.05",
            },
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    summary, executions = build_report(path)
    item = summary["DFOK-X"]
    assert item["events"] == 1
    assert item["wins"] == 1
    assert item["placements"] == 1
    assert item["dual_opportunities"] == 1
    assert item["dual_avg_lifetime_ms"] == Decimal("25")
    assert executions[0]["arrival_skew_ms"] == 25
    assert executions[0]["detected_edge_per_share"] == "0.01"
