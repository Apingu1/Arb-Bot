from decimal import Decimal

import arb_bot.ev_frontier as ev_module
import arb_bot.hedgeable_research as hedge_module
from arb_bot.config import Settings
from arb_bot.discovery import MarketPhase
from arb_bot.ev_frontier import FrontierHedgeVariant, SplitSellVariant
from arb_bot.maker_research import MarketRegimeTracker, SurgeSnapshot
from arb_bot.models import MarketPair
from arb_bot.storage import JsonlRecorder
from arb_bot.strategy import ArbitrageEngine


def _settings(**overrides):
    base = dict(
        min_expected_profit_usdc=Decimal("0.01"),
        max_trade_shares=Decimal("50"),
        maker_tick_size=Decimal("0.01"),
        hedge_enabled=True,
        hedge_latency_reserve_per_share=Decimal("0"),
        hedge_min_expected_profit_usdc=Decimal("0.01"),
        hedge_max_improve_ticks=0,
        hedge_max_quote_age_ms=30000,
        hedge_requote_cooldown_ms=0,
        hedge_min_seconds_to_expiry=0,
        shadow_recovery_latency_ms=100,
        hedge_ghost_enabled=True,
        hedge_ghost_horizon_ms=10000,
        hedge_ghost_max_active=100,
        ev_frontier_enabled=True,
        ev_hard_loss_per_share=Decimal("0.20"),
        split_sell_enabled=True,
        split_sell_shares=Decimal("5"),
        split_sell_max_quote_age_ms=30000,
        split_sell_inventory_timeout_ms=1000,
        split_sell_requote_cooldown_ms=0,
        split_sell_min_seconds_to_expiry=0,
        surge_move_1s=Decimal("1"),
        surge_move_3s=Decimal("1"),
        surge_updates_per_second=100000,
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


def _engine(settings, *, a_bid="0.40", a_ask="0.41", b_bid="0.50", b_ask="0.55", depth="100"):
    engine = ArbitrageEngine(settings)
    engine.set_markets([_pair()])
    engine.books["A"].apply_snapshot(
        [{"price": a_bid, "size": "10"}],
        [{"price": a_ask, "size": depth}],
    )
    engine.books["B"].apply_snapshot(
        [{"price": b_bid, "size": "10"}],
        [{"price": b_ask, "size": depth}],
    )
    return engine


def _variant(settings, tmp_path, *, grace=0, size=Decimal("5"), name="EV-TEST"):
    recorder = JsonlRecorder(str(tmp_path / f"{name}.jsonl"))
    regime = MarketRegimeTracker(settings)
    return FrontierHedgeVariant(
        settings,
        recorder,
        regime,
        edge_target=Decimal("0.005"),
        completion_latency_ms=0,
        grace_ms=grace,
        fixed_size=size,
        strategy_name=name,
    )


def test_grace_variant_waits_before_cancelling(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(ev_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(hedge_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(ev_module, "market_phase", lambda pair: MarketPhase.LIVE)
    settings = _settings()
    engine = _engine(settings)
    variant = _variant(settings, tmp_path, grace=250, name="EV-G250")

    variant.on_market_update(engine, "m-live", SurgeSnapshot(False, ()))
    campaign = variant.campaigns["m-live"]
    hedge_token = "B" if campaign.hedge_side == "B" else "A"
    hedge_book = engine.books[hedge_token]
    hedge_book.apply_snapshot(
        [{"price": "0.01", "size": "100"}],
        [{"price": "0.70", "size": "100"}],
    )

    variant.on_market_update(engine, "m-live", SurgeSnapshot(False, ()))
    assert "m-live" in variant.campaigns

    clock[0] += 0.20
    variant.process_due(engine)
    assert "m-live" in variant.campaigns

    clock[0] += 0.06
    variant.process_due(engine)
    assert "m-live" not in variant.campaigns
    assert variant.cancel_reasons["HEDGEABILITY_GRACE_EXPIRED"] == 1


def test_cancelled_order_is_followed_as_non_pnl_ghost(tmp_path, monkeypatch):
    monkeypatch.setattr(ev_module, "market_phase", lambda pair: MarketPhase.LIVE)
    settings = _settings()
    engine = _engine(settings)
    variant = _variant(settings, tmp_path, grace=0, name="EV-GHOST")

    variant.on_market_update(engine, "m-live", SurgeSnapshot(False, ()))
    campaign = variant.campaigns["m-live"]
    maker_token = "A" if campaign.maker_side == "A" else "B"
    hedge_token = "B" if campaign.hedge_side == "B" else "A"

    # Force strict hedgeability cancellation.
    engine.books[hedge_token].apply_snapshot(
        [{"price": "0.01", "size": "100"}],
        [{"price": "0.70", "size": "100"}],
    )
    variant.on_market_update(engine, "m-live", SurgeSnapshot(False, ()))
    assert variant.ghost_created == 1
    assert variant.total_pnl == Decimal("0")

    # Restore hedge liquidity, then trade enough SELL volume through the
    # cancelled maker price to show that the ghost order would have filled.
    engine.books[hedge_token].apply_snapshot(
        [{"price": "0.50", "size": "100"}],
        [{"price": "0.55", "size": "100"}],
    )
    ghost = variant.ghosts["m-live"][0]
    engine.books[maker_token].apply_trade(
        str(ghost.maker_price),
        str(ghost.queue_ahead + ghost.shares),
        "SELL",
        "ghost-fill",
    )
    variant.on_market_update(engine, "m-live", SurgeSnapshot(False, ()))

    assert variant.ghost_filled == 1
    assert variant.total_pnl == Decimal("0")


def test_fixed_size_variant_places_exact_requested_size(tmp_path, monkeypatch):
    monkeypatch.setattr(ev_module, "market_phase", lambda pair: MarketPhase.LIVE)
    settings = _settings()
    engine = _engine(settings, depth="500")
    variant = _variant(settings, tmp_path, grace=100, size=Decimal("10"), name="EV-S10")

    variant.on_market_update(engine, "m-live", SurgeSnapshot(False, ()))

    assert variant.campaigns["m-live"].target_shares == Decimal("10")


def test_split_sell_both_passive_legs_can_complete_profitably(tmp_path, monkeypatch):
    monkeypatch.setattr(ev_module, "market_phase", lambda pair: MarketPhase.LIVE)
    settings = _settings()
    engine = _engine(settings, a_bid="0.49", a_ask="0.50", b_bid="0.50", b_ask="0.51")
    recorder = JsonlRecorder(str(tmp_path / "split.jsonl"))
    regime = MarketRegimeTracker(settings)
    variant = SplitSellVariant(settings, recorder, regime, Decimal("0.005"))

    variant.on_market_update(engine, "m-live", SurgeSnapshot(False, ()))
    campaign = variant.campaigns["m-live"]
    engine.books["A"].apply_trade(
        str(campaign.ask_a),
        str(campaign.leg_a.queue_ahead + campaign.target_shares),
        "BUY",
        "sell-a",
    )
    engine.books["B"].apply_trade(
        str(campaign.ask_b),
        str(campaign.leg_b.queue_ahead + campaign.target_shares),
        "BUY",
        "sell-b",
    )
    variant.on_market_update(engine, "m-live", SurgeSnapshot(False, ()))

    assert variant.completed == 1
    assert variant.total_pnl > Decimal("0")
    assert "m-live" not in variant.campaigns
