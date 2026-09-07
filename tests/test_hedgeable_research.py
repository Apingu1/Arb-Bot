from decimal import Decimal

import arb_bot.hedgeable_research as hedge_module
from arb_bot.config import Settings
from arb_bot.discovery import MarketPhase
from arb_bot.hedgeable_research import HedgeableResearchSuite, HedgeableVariantEngine
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
        hedge_net_edge_targets=(Decimal("0.005"),),
        hedge_completion_latencies_ms=(0,),
        hedge_size_candidates=(Decimal("5"),),
        hedge_latency_reserve_per_share=Decimal("0"),
        hedge_min_expected_profit_usdc=Decimal("0.01"),
        hedge_max_improve_ticks=0,
        hedge_max_quote_age_ms=30000,
        hedge_requote_cooldown_ms=0,
        hedge_min_seconds_to_expiry=0,
        shadow_recovery_latency_ms=100,
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


def _variant(settings, tmp_path, *, edge=Decimal("0.005"), latency=0):
    recorder = JsonlRecorder(str(tmp_path / "events.jsonl"))
    regime = MarketRegimeTracker(settings)
    return HedgeableVariantEngine(
        settings,
        recorder,
        regime,
        edge_target=edge,
        completion_latency_ms=latency,
    )


def _fill_current_maker_order(engine, campaign):
    token = "A" if campaign.maker_side == "A" else "B"
    # Consume all displayed queue ahead and enough overflow to fill our order.
    trade_size = campaign.queue.queue_ahead + campaign.target_shares
    engine.books[token].apply_trade(str(campaign.maker_price), str(trade_size), "SELL", "fill")


def test_hedgeable_maker_fill_completes_with_one_taker_leg(tmp_path, monkeypatch):
    monkeypatch.setattr(hedge_module, "market_phase", lambda pair: MarketPhase.LIVE)
    settings = _settings()
    engine = _engine(settings)
    variant = _variant(settings, tmp_path)

    variant.on_market_update(engine, "m-live", SurgeSnapshot(False, ()))
    campaign = variant.campaigns["m-live"]
    assert campaign.placement_expected_edge >= Decimal("0.005")

    _fill_current_maker_order(engine, campaign)
    variant.on_market_update(engine, "m-live", SurgeSnapshot(False, ()))
    assert variant.maker_fills == 1
    assert variant.campaigns["m-live"].pending_hedge is not None

    variant.process_due(engine)

    assert variant.hedge_successes == 1
    assert variant.total_pnl > Decimal("0")
    assert variant.last_summary is not None
    assert variant.last_summary["status"] == "HEDGE_COMPLETED"
    assert variant.last_summary["maker_fill_qty"] == Decimal("5")


def test_resting_maker_quote_cancels_when_opposite_hedge_deteriorates(tmp_path, monkeypatch):
    monkeypatch.setattr(hedge_module, "market_phase", lambda pair: MarketPhase.LIVE)
    settings = _settings()
    engine = _engine(settings)
    variant = _variant(settings, tmp_path)

    variant.on_market_update(engine, "m-live", SurgeSnapshot(False, ()))
    campaign = variant.campaigns["m-live"]
    hedge_token = "B" if campaign.hedge_side == "B" else "A"
    book = engine.books[hedge_token]
    bid = book.best_bid() or Decimal("0.01")
    book.apply_snapshot(
        [{"price": str(bid), "size": "100"}],
        [{"price": "0.99", "size": "100"}],
    )

    variant.on_market_update(engine, "m-live", SurgeSnapshot(False, ()))

    assert variant.cancelled == 1
    assert "m-live" not in variant.campaigns


def test_hedge_latency_miss_enters_delayed_recovery_and_unwinds(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(hedge_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(hedge_module, "market_phase", lambda pair: MarketPhase.LIVE)
    settings = _settings(hedge_completion_latencies_ms=(100,))
    engine = _engine(settings)
    variant = _variant(settings, tmp_path, latency=100)

    variant.on_market_update(engine, "m-live", SurgeSnapshot(False, ()))
    campaign = variant.campaigns["m-live"]
    _fill_current_maker_order(engine, campaign)
    variant.on_market_update(engine, "m-live", SurgeSnapshot(False, ()))
    campaign = variant.campaigns["m-live"]
    assert campaign.pending_hedge is not None

    # Move the missing taker leg above the original FOK limit before 100ms.
    hedge_token = "B" if campaign.hedge_side == "B" else "A"
    hedge_book = engine.books[hedge_token]
    hedge_bid = hedge_book.best_bid() or Decimal("0.01")
    hedge_book.apply_snapshot(
        [{"price": str(hedge_bid), "size": "100"}],
        [{"price": "0.99", "size": "100"}],
    )
    clock[0] += 0.11
    variant.process_due(engine)

    assert variant.hedge_misses == 1
    campaign = variant.campaigns["m-live"]
    assert campaign.pending_recovery is not None

    clock[0] += 0.11
    variant.process_due(engine)

    assert variant.last_summary is not None
    assert variant.last_summary["status"] in {"HEDGE_RECOVERY_UNWIND", "HEDGE_RECOVERY_COMPLETE"}
    assert variant.hedge_successes == 0


def test_extreme_regime_is_recorded_from_probability_state(tmp_path, monkeypatch):
    monkeypatch.setattr(hedge_module, "market_phase", lambda pair: MarketPhase.LIVE)
    settings = _settings()
    engine = _engine(settings, a_bid="0.95", a_ask="0.96", b_bid="0.04", b_ask="0.05")
    variant = _variant(settings, tmp_path)

    assert variant._market_regime(engine, _pair()) == "EXTREME"


def test_suite_builds_edge_by_latency_matrix(tmp_path):
    settings = _settings(
        hedge_net_edge_targets=(Decimal("0.005"), Decimal("0.010"), Decimal("0.015"), Decimal("0.020")),
        hedge_completion_latencies_ms=(50, 100, 200),
    )
    recorder = JsonlRecorder(str(tmp_path / "matrix.jsonl"))
    regime = MarketRegimeTracker(settings)
    suite = HedgeableResearchSuite(settings, recorder, regime)

    assert len(suite.variants) == 12
    assert {variant.completion_latency_ms for variant in suite.variants} == {50, 100, 200}
    assert {variant.edge_target for variant in suite.variants} == {
        Decimal("0.005"), Decimal("0.010"), Decimal("0.015"), Decimal("0.020")
    }
