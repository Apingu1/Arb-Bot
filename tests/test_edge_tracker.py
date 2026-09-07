import json
from datetime import datetime, timezone
from decimal import Decimal

from arb_bot.config import Settings
from arb_bot.edge_tracker import EdgeTracker
from arb_bot.models import MarketPair
from arb_bot.storage import JsonlRecorder
from arb_bot.strategy import ArbitrageEngine


def _settings(**overrides):
    base = dict(
        min_net_edge_per_share=Decimal("0.001"),
        min_expected_profit_usdc=Decimal("0.01"),
        min_trade_shares=Decimal("5"),
        max_trade_shares=Decimal("100"),
        risk_buffer_per_share=Decimal("0"),
        max_book_age_ms=10000,
    )
    base.update(overrides)
    return Settings(**base)


def _live_pair():
    return MarketPair(
        market_id="m-live",
        condition_id="c-live",
        slug="btc-updown-15m-1788759000",
        question="BTC Up or Down 15m",
        outcome_a="Up",
        outcome_b="Down",
        token_a="UP",
        token_b="DOWN",
        end_date="2026-09-07T05:45:00Z",
    )


def test_edge_tracker_records_unqualified_and_best_observed_pair(tmp_path):
    engine = ArbitrageEngine(_settings())
    engine.set_markets([_live_pair()])
    engine.books["UP"].apply_snapshot(
        [{"price": "0.48", "size": "100"}],
        [{"price": "0.51", "size": "100"}],
    )
    engine.books["DOWN"].apply_snapshot(
        [{"price": "0.49", "size": "100"}],
        [{"price": "0.50", "size": "100"}],
    )

    output = tmp_path / "events.jsonl"
    tracker = EdgeTracker(JsonlRecorder(str(output)))
    now = datetime(2026, 9, 7, 5, 43, tzinfo=timezone.utc)

    observation = tracker.observe(
        engine,
        "m-live",
        source_event="book",
        exchange_timestamp="123",
        now_utc=now,
    )

    assert observation is not None
    assert observation["phase"] == "LIVE"
    assert observation["top_pair_price"] == Decimal("1.01")
    assert observation["raw_edge_per_share"] == Decimal("-0.01")
    assert observation["qualifies_shadow"] is False

    stats = tracker.summary(_live_pair().slug)
    assert stats is not None
    assert stats.observations == 1
    assert stats.best_pair_price == Decimal("1.01")

    row = json.loads(output.read_text().strip())
    assert row["event_type"] == "edge_observation"
    assert row["payload"]["slug"] == _live_pair().slug


def test_edge_tracker_updates_best_net_edge_for_profitable_gap(tmp_path):
    engine = ArbitrageEngine(_settings())
    engine.set_markets([_live_pair()])
    engine.books["UP"].apply_snapshot([], [{"price": "0.47", "size": "100"}])
    engine.books["DOWN"].apply_snapshot([], [{"price": "0.47", "size": "100"}])

    tracker = EdgeTracker(JsonlRecorder(str(tmp_path / "events.jsonl")))
    observation = tracker.observe(
        engine,
        "m-live",
        source_event="price_change",
        now_utc=datetime(2026, 9, 7, 5, 43, tzinfo=timezone.utc),
    )

    assert observation is not None
    assert observation["raw_edge_per_share"] == Decimal("0.06")
    assert observation["net_edge_per_share"] > Decimal("0")
    assert observation["qualifies_shadow"] is True

    stats = tracker.summary(_live_pair().slug)
    assert stats is not None
    assert stats.raw_positive == 1
    assert stats.net_positive == 1
    assert stats.qualifying == 1
    assert stats.best_pair_price == Decimal("0.94")
