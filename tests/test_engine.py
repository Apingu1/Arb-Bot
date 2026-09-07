from decimal import Decimal

from arb_bot.config import Settings
from arb_bot.fees import taker_fee
from arb_bot.models import FillSegment, MarketPair
from arb_bot.strategy import ArbitrageEngine


def settings(**overrides):
    base = dict(min_net_edge_per_share=Decimal("0.001"), min_expected_profit_usdc=Decimal("0.01"), min_trade_shares=Decimal("10"), max_trade_shares=Decimal("100"), risk_buffer_per_share=Decimal("0"), max_book_age_ms=10000)
    base.update(overrides)
    return Settings(**base)


def pair():
    return MarketPair("m1", "c1", "btc-up-down", "Bitcoin Up or Down?", "Up", "Down", "A", "B")


def seed(engine: ArbitrageEngine, ask_a: str, ask_b: str, size: str = "100"):
    engine.set_markets([pair()])
    engine.books["A"].apply_snapshot([], [{"price": ask_a, "size": size}])
    engine.books["B"].apply_snapshot([], [{"price": ask_b, "size": size}])


def test_fee_matches_polymarket_crypto_table_at_midpoint():
    fee = taker_fee((FillSegment(Decimal("0.50"), Decimal("100")),))
    assert fee == Decimal("1.750000")


def test_97_cent_midpoint_pair_is_not_profitable_after_crypto_taker_fees():
    engine = ArbitrageEngine(settings())
    seed(engine, "0.49", "0.48")
    assert engine.evaluate("m1") is None


def test_94_cent_pair_can_clear_fee_adjusted_threshold():
    engine = ArbitrageEngine(settings())
    seed(engine, "0.47", "0.47")
    opportunity = engine.evaluate("m1")
    assert opportunity is not None
    assert opportunity.expected_net_profit > 0


def test_full_depth_changes_real_pair_cost():
    engine = ArbitrageEngine(settings(max_trade_shares=Decimal("100")))
    engine.set_markets([pair()])
    engine.books["A"].apply_snapshot([], [{"price": "0.44", "size": "10"}, {"price": "0.50", "size": "90"}])
    engine.books["B"].apply_snapshot([], [{"price": "0.48", "size": "10"}, {"price": "0.51", "size": "90"}])
    opportunity = engine.evaluate("m1")
    assert opportunity is not None
    assert opportunity.shares == Decimal("10")
