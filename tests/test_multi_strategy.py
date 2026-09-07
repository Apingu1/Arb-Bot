from decimal import Decimal

import arb_bot.hybrid_shadow as hybrid_module
import arb_bot.maker_shadow as maker_module
import arb_bot.simulator as simulator_module
import arb_bot.strategy as strategy_module
from arb_bot.config import Settings
from arb_bot.discovery import MarketPhase
from arb_bot.hybrid_shadow import HybridShadowEngine
from arb_bot.maker_shadow import MakerShadowEngine
from arb_bot.models import MarketPair
from arb_bot.simulator import ShadowExecutor
from arb_bot.storage import JsonlRecorder
from arb_bot.strategy import ArbitrageEngine
from arb_bot.strategy_metrics import EmpiricalLegRisk


def _settings(**overrides):
    base = dict(
        min_net_edge_per_share=Decimal("0.001"),
        min_expected_profit_usdc=Decimal("0.01"),
        min_trade_shares=Decimal("5"),
        max_trade_shares=Decimal("5"),
        risk_buffer_per_share=Decimal("0"),
        recovery_penalty_per_share=Decimal("0"),
        shadow_latency_ms=0,
        shadow_recovery_latency_ms=100,
        market_cooldown_ms=0,
        max_book_age_ms=10000,
        maker_trade_shares=Decimal("5"),
        maker_min_gross_edge_per_share=Decimal("0.005"),
        maker_order_ttl_ms=1500,
        maker_inventory_timeout_ms=2500,
        hybrid_trade_shares=Decimal("5"),
        hybrid_min_net_edge_per_share=Decimal("0.003"),
        hybrid_completion_latency_ms=0,
        hybrid_inventory_timeout_ms=2500,
    )
    base.update(overrides)
    return Settings(**base)


def _pair():
    return MarketPair(
        market_id="m-live",
        condition_id="c-live",
        slug="btc-updown-15m-1788767100",
        question="BTC Up or Down 15m",
        outcome_a="Up",
        outcome_b="Down",
        token_a="A",
        token_b="B",
    )


def _engine(settings):
    engine = ArbitrageEngine(settings)
    engine.set_markets([_pair()])
    return engine


def test_taker_leg_miss_waits_for_recovery_latency_and_uses_later_book(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(simulator_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(strategy_module.time, "monotonic", lambda: clock[0])

    settings = _settings()
    engine = _engine(settings)
    engine.books["A"].apply_snapshot(
        [{"price": "0.44", "size": "100"}],
        [{"price": "0.45", "size": "100"}],
    )
    engine.books["B"].apply_snapshot(
        [{"price": "0.44", "size": "100"}],
        [{"price": "0.45", "size": "100"}],
    )
    opportunity = engine.evaluate("m-live")
    assert opportunity is not None

    taker = ShadowExecutor(settings, JsonlRecorder(str(tmp_path / "events.jsonl")))
    assert taker.submit(opportunity)

    # By simulated execution time only A remains inside the detected FOK limit.
    engine.books["B"].apply_snapshot(
        [{"price": "0.39", "size": "100"}],
        [{"price": "0.55", "size": "100"}],
    )
    assert taker.process_due(engine.books) == []
    assert "m-live" in taker.recoveries
    assert taker.leg_misses == 0

    clock[0] += 0.05
    assert taker.process_due(engine.books) == []
    assert taker.total_pnl == Decimal("0")

    # Change the recovery book again before the configured 100ms recovery delay.
    engine.books["B"].apply_snapshot(
        [{"price": "0.38", "size": "100"}],
        [{"price": "0.60", "size": "100"}],
    )
    clock[0] += 0.06
    results = taker.process_due(engine.books)

    assert len(results) == 1
    assert results[0].status == "ONE_LEG_MISS"
    assert taker.leg_misses == 1
    assert taker.last_summary is not None
    assert taker.last_summary["recovery"]["actual_recovery_latency_ms"] >= Decimal("100")
    assert taker.last_summary["recovery"]["completion_quote"]["average_price"] == Decimal("0.60")
    assert taker.last_summary["initial_execution"]["leg_a"]["average_price"] == Decimal("0.45")


def test_last_trade_event_is_routed_to_token_book():
    settings = _settings()
    engine = _engine(settings)
    touched = engine.apply_event(
        {
            "event_type": "last_trade_price",
            "asset_id": "A",
            "price": "0.48",
            "size": "7",
            "side": "SELL",
            "timestamp": "123",
        }
    )
    assert touched == "m-live"
    assert engine.books["A"].last_trade_price == Decimal("0.48")
    assert engine.books["A"].last_trade_size == Decimal("7")
    assert engine.books["A"].last_trade_side == "SELL"


def test_maker_engine_requires_confirming_sell_trades_on_both_legs(tmp_path, monkeypatch):
    monkeypatch.setattr(maker_module, "market_phase", lambda pair: MarketPhase.LIVE)
    settings = _settings()
    engine = _engine(settings)
    engine.books["A"].apply_snapshot(
        [{"price": "0.48", "size": "100"}],
        [{"price": "0.49", "size": "100"}],
    )
    engine.books["B"].apply_snapshot(
        [{"price": "0.50", "size": "100"}],
        [{"price": "0.51", "size": "100"}],
    )
    maker = MakerShadowEngine(settings, JsonlRecorder(str(tmp_path / "maker.jsonl")))

    maker.on_market_update(engine, "m-live")
    assert maker.placed == 1
    assert maker.completed == 0

    engine.books["A"].apply_trade("0.48", "5", "SELL", "trade-a")
    maker.on_market_update(engine, "m-live")
    assert maker.completed == 0

    engine.books["B"].apply_trade("0.50", "5", "SELL", "trade-b")
    maker.on_market_update(engine, "m-live")

    assert maker.completed == 1
    assert maker.total_pnl == Decimal("0.10")
    assert maker.equity.equity == Decimal("0.10")


def test_maker_does_not_credit_small_or_wrong_side_trade(tmp_path, monkeypatch):
    monkeypatch.setattr(maker_module, "market_phase", lambda pair: MarketPhase.LIVE)
    settings = _settings()
    engine = _engine(settings)
    engine.books["A"].apply_snapshot(
        [{"price": "0.48", "size": "100"}],
        [{"price": "0.49", "size": "100"}],
    )
    engine.books["B"].apply_snapshot(
        [{"price": "0.50", "size": "100"}],
        [{"price": "0.51", "size": "100"}],
    )
    maker = MakerShadowEngine(settings, JsonlRecorder(str(tmp_path / "maker-filter.jsonl")))
    maker.on_market_update(engine, "m-live")

    engine.books["A"].apply_trade("0.48", "4.99", "SELL", "too-small")
    maker.on_market_update(engine, "m-live")
    assert maker.campaigns["m-live"].filled_a is False

    engine.books["A"].apply_trade("0.48", "10", "BUY", "wrong-side")
    maker.on_market_update(engine, "m-live")
    assert maker.campaigns["m-live"].filled_a is False


def test_hybrid_maker_first_then_single_taker_leg_can_complete(tmp_path, monkeypatch):
    monkeypatch.setattr(hybrid_module, "market_phase", lambda pair: MarketPhase.LIVE)
    settings = _settings()
    engine = _engine(settings)
    engine.books["A"].apply_snapshot(
        [{"price": "0.40", "size": "100"}],
        [{"price": "0.41", "size": "100"}],
    )
    engine.books["B"].apply_snapshot(
        [{"price": "0.50", "size": "100"}],
        [{"price": "0.55", "size": "100"}],
    )
    hybrid = HybridShadowEngine(settings, JsonlRecorder(str(tmp_path / "hybrid.jsonl")))

    hybrid.on_market_update(engine, "m-live")
    assert hybrid.placed == 1

    # A maker leg is confirmed by a SELL trade at our bid; B remains above its
    # maker bid but is cheap enough to complete as a single taker leg after fees.
    engine.books["A"].apply_trade("0.40", "5", "SELL", "maker-a")
    hybrid.on_market_update(engine, "m-live")
    assert hybrid.campaigns["m-live"].filled_a is True
    assert hybrid.campaigns["m-live"].completion is not None

    hybrid.process_due(engine)

    assert hybrid.completed == 1
    assert hybrid.completed_with_taker == 1
    assert hybrid.total_pnl > Decimal("0")
    assert hybrid.equity.equity == hybrid.total_pnl


def test_empirical_leg_risk_estimate_uses_observed_miss_frequency_and_loss():
    risk = EmpiricalLegRisk()
    risk.observe(status="BOTH_FILLED", pnl=Decimal("0.25"), shares=Decimal("5"))
    risk.observe(status="NEITHER_FILLED", pnl=Decimal("0"), shares=Decimal("5"))
    risk.observe(status="ONE_LEG_MISS", pnl=Decimal("-1.00"), shares=Decimal("5"))
    risk.observe(status="ONE_LEG_MISS", pnl=Decimal("-0.50"), shares=Decimal("5"))

    assert risk.attempts == 4
    assert risk.miss_probability == Decimal("0.5")
    assert risk.average_miss_loss == Decimal("0.75")
    assert risk.average_miss_loss_per_share == Decimal("0.15")
    assert risk.estimated_reserve_per_share == Decimal("0.075")


def test_strategy_equity_curves_are_independent(tmp_path):
    settings = _settings()
    recorder = JsonlRecorder(str(tmp_path / "all.jsonl"))
    taker = ShadowExecutor(settings, recorder)
    maker = MakerShadowEngine(settings, recorder)
    hybrid = HybridShadowEngine(settings, recorder)

    taker.equity.apply(Decimal("-1"), market_id="m", slug="s", status="LOSS", action="TEST")
    maker.equity.apply(Decimal("0.5"), market_id="m", slug="s", status="WIN", action="TEST")
    hybrid.equity.apply(Decimal("0.2"), market_id="m", slug="s", status="WIN", action="TEST")

    assert taker.equity.equity == Decimal("-1")
    assert maker.equity.equity == Decimal("0.5")
    assert hybrid.equity.equity == Decimal("0.2")
