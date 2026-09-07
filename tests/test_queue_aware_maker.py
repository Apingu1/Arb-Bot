from decimal import Decimal

import arb_bot.maker_research as research_module
from arb_bot.config import Settings
from arb_bot.discovery import MarketPhase
from arb_bot.maker_research import MakerResearchSuite, QueueAwareVariantEngine, target_bids
from arb_bot.models import MarketPair
from arb_bot.storage import JsonlRecorder
from arb_bot.strategy import ArbitrageEngine


def _settings(**overrides):
    base = dict(
        min_expected_profit_usdc=Decimal("0.01"),
        maker_trade_shares=Decimal("5"),
        hybrid_trade_shares=Decimal("5"),
        maker_variant_targets=(Decimal("0.99"), Decimal("0.98"), Decimal("0.97"), Decimal("0.96")),
        maker_tick_size=Decimal("0.01"),
        maker_min_gross_edge_per_share=Decimal("0.005"),
        maker_min_seconds_to_expiry=0,
        maker_max_quote_age_ms=30000,
        maker_reprice_ticks=2,
        maker_requote_cooldown_ms=0,
        maker_inventory_timeout_ms=5000,
        hybrid_inventory_timeout_ms=5000,
        hybrid_min_net_edge_per_share=Decimal("0.001"),
        hybrid_completion_latency_ms=0,
        surge_updates_per_second=999999,
        surge_move_1s=Decimal("1"),
        surge_move_3s=Decimal("1"),
        surge_one_sided_count=999,
    )
    base.update(overrides)
    return Settings(**base)


def _pair():
    return MarketPair(
        market_id="m",
        condition_id="c",
        slug="btc-updown-15m-1893456000",
        question="BTC Up or Down",
        outcome_a="Up",
        outcome_b="Down",
        token_a="A",
        token_b="B",
    )


def _engine(settings, bid_a="0.48", ask_a="0.49", bid_b="0.51", ask_b="0.52", size="100"):
    engine = ArbitrageEngine(settings)
    engine.set_markets([_pair()])
    engine.books["A"].apply_snapshot([{"price": bid_a, "size": size}], [{"price": ask_a, "size": size}])
    engine.books["B"].apply_snapshot([{"price": bid_b, "size": size}], [{"price": ask_b, "size": size}])
    return engine


def test_target_bids_create_distinct_99_98_97_96_experiments():
    a99, b99 = target_bids(Decimal("0.48"), Decimal("0.51"), Decimal("0.99"), Decimal("0.01"))
    a98, b98 = target_bids(Decimal("0.48"), Decimal("0.51"), Decimal("0.98"), Decimal("0.01"))
    a97, b97 = target_bids(Decimal("0.48"), Decimal("0.51"), Decimal("0.97"), Decimal("0.01"))
    a96, b96 = target_bids(Decimal("0.48"), Decimal("0.51"), Decimal("0.96"), Decimal("0.01"))

    assert a99 + b99 == Decimal("0.99")
    assert a98 + b98 <= Decimal("0.98")
    assert a97 + b97 <= Decimal("0.97")
    assert a96 + b96 <= Decimal("0.96")
    assert len({(a99, b99), (a98, b98), (a97, b97), (a96, b96)}) == 4


def test_equal_price_sell_volume_must_consume_queue_before_virtual_fill(tmp_path, monkeypatch):
    monkeypatch.setattr(research_module, "market_phase", lambda pair: MarketPhase.LIVE)
    settings = _settings(maker_variant_targets=(Decimal("0.99"),))
    engine = _engine(settings, size="100")
    recorder = JsonlRecorder(str(tmp_path / "queue.jsonl"))
    suite = MakerResearchSuite(settings, recorder)
    maker = suite.makers[0]

    suite.on_market_update(engine, "m")
    campaign = maker.campaigns["m"]
    assert campaign.leg_a.queue_ahead == Decimal("100")

    # 60 shares sell at our price: they consume queue ahead, not our order.
    engine.books["A"].apply_trade(str(campaign.leg_a.price), "60", "SELL", "t1")
    suite.on_market_update(engine, "m")
    assert campaign.leg_a.filled_qty == Decimal("0")
    assert campaign.leg_a.queue_ahead == Decimal("40")

    # Another 42 shares consume the final 40 ahead and fill only 2 of our 5.
    engine.books["A"].apply_trade(str(campaign.leg_a.price), "42", "SELL", "t2")
    suite.on_market_update(engine, "m")
    assert campaign.leg_a.filled_qty == Decimal("2")
    assert campaign.leg_a.queue_ahead == Decimal("0")

    # The remaining 3 shares then fill.
    engine.books["A"].apply_trade(str(campaign.leg_a.price), "3", "SELL", "t3")
    suite.on_market_update(engine, "m")
    assert campaign.leg_a.filled_qty == Decimal("5")


def test_trade_through_resting_bid_fills_remaining_order(tmp_path, monkeypatch):
    monkeypatch.setattr(research_module, "market_phase", lambda pair: MarketPhase.LIVE)
    settings = _settings(maker_variant_targets=(Decimal("0.99"),))
    engine = _engine(settings)
    suite = MakerResearchSuite(settings, JsonlRecorder(str(tmp_path / "through.jsonl")))
    maker = suite.makers[0]
    suite.on_market_update(engine, "m")
    campaign = maker.campaigns["m"]

    engine.books["A"].apply_trade(str(campaign.leg_a.price - Decimal("0.01")), "1", "SELL", "through")
    suite.on_market_update(engine, "m")

    assert campaign.leg_a.filled_qty == Decimal("5")
    assert campaign.leg_a.queue_ahead == Decimal("0")


def test_variants_have_independent_queues_and_equity(tmp_path, monkeypatch):
    monkeypatch.setattr(research_module, "market_phase", lambda pair: MarketPhase.LIVE)
    settings = _settings(maker_variant_targets=(Decimal("0.99"), Decimal("0.97")))
    engine = _engine(settings)
    suite = MakerResearchSuite(settings, JsonlRecorder(str(tmp_path / "independent.jsonl")))
    suite.on_market_update(engine, "m")

    maker99, maker97 = suite.makers
    c99 = maker99.campaigns["m"]
    c97 = maker97.campaigns["m"]
    assert (c99.leg_a.price, c99.leg_b.price) != (c97.leg_a.price, c97.leg_b.price)

    # A trade at the deeper 97 variant price fills/trades through MAKER-97's A
    # order, while the independent 99 queue still needs its own queue volume.
    engine.books["A"].apply_trade(str(c97.leg_a.price), "5", "SELL", "variant-fill")
    suite.on_market_update(engine, "m")

    assert c97.leg_a.filled_qty == Decimal("5")
    assert c99.leg_a.filled_qty == Decimal("5")  # price traded through 99 too
    # They observed the same external event, but have distinct campaign state.
    assert c97 is not c99
    assert maker97.equity is not maker99.equity


def test_hybrid_reprices_missing_leg_only_up_to_profitability_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(research_module, "market_phase", lambda pair: MarketPhase.LIVE)
    settings = _settings(maker_variant_targets=(Decimal("0.97"),), hybrid_min_reprice_interval_ms=0)
    engine = _engine(settings, bid_a="0.40", ask_a="0.41", bid_b="0.57", ask_b="0.58", size="0")
    # Re-seed usable asks and no queue at the virtual bid levels.
    engine.books["A"].apply_snapshot([{"price": "0.40", "size": "0"}], [{"price": "0.41", "size": "100"}])
    engine.books["B"].apply_snapshot([{"price": "0.57", "size": "0"}], [{"price": "0.58", "size": "100"}])
    # Add top bids with no queue at the eventual target level.
    engine.books["A"].bids = {Decimal("0.40"): Decimal("0")}
    engine.books["B"].bids = {Decimal("0.57"): Decimal("0")}

    suite = MakerResearchSuite(settings, JsonlRecorder(str(tmp_path / "hybrid-cap.jsonl")))
    hybrid = suite.hybrids[0]
    suite.on_market_update(engine, "m")
    campaign = hybrid.campaigns["m"]

    # Force a trade-through fill on A. A=0.40 means B can never be repriced
    # above 0.57 for a 0.97 target.
    engine.books["A"].apply_trade("0.39", "5", "SELL", "a-fill")
    suite.on_market_update(engine, "m")
    assert campaign.leg_a.filled_qty == Decimal("5")

    engine.books["B"].bids = {Decimal("0.65"): Decimal("100")}
    engine.books["B"].asks = {Decimal("0.66"): Decimal("100")}
    suite.on_market_update(engine, "m")

    assert campaign.leg_b.price <= Decimal("0.57")


def test_suite_builds_four_maker_and_four_hybrid_variants(tmp_path):
    settings = _settings()
    suite = MakerResearchSuite(settings, JsonlRecorder(str(tmp_path / "suite.jsonl")))
    assert [item.strategy_name for item in suite.makers] == ["MAKER-99", "MAKER-98", "MAKER-97", "MAKER-96"]
    assert [item.strategy_name for item in suite.hybrids] == ["HYBRID-99", "HYBRID-98", "HYBRID-97", "HYBRID-96"]
