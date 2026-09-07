from datetime import datetime, timezone

from arb_bot.discovery import (
    MarketDiscovery,
    MarketPhase,
    btc_15m_candidate_slugs,
    btc_15m_window_from_slug,
    classify_btc_15m_slug,
    select_live_and_next_pairs,
)
from arb_bot.models import MarketPair


def test_btc_15m_candidate_slugs_are_aligned_to_current_window():
    now = datetime(2026, 9, 7, 5, 43, tzinfo=timezone.utc)
    slugs = btc_15m_candidate_slugs(now, lookback_intervals=1, lookahead_intervals=1)

    assert slugs == [
        "btc-updown-15m-1788758100",
        "btc-updown-15m-1788759000",
        "btc-updown-15m-1788759900",
    ]


def test_btc_15m_window_is_derived_from_slug_timestamp():
    start, end = btc_15m_window_from_slug("btc-updown-15m-1788759900")
    assert start == datetime(2026, 9, 7, 5, 45, tzinfo=timezone.utc)
    assert end == datetime(2026, 9, 7, 6, 0, tzinfo=timezone.utc)


def test_classifies_live_next_future_expired():
    now = datetime(2026, 9, 7, 6, 34, tzinfo=timezone.utc)
    assert classify_btc_15m_slug("btc-updown-15m-1788762600", now) is MarketPhase.LIVE
    assert classify_btc_15m_slug("btc-updown-15m-1788763500", now) is MarketPhase.NEXT
    assert classify_btc_15m_slug("btc-updown-15m-1788764400", now) is MarketPhase.FUTURE
    assert classify_btc_15m_slug("btc-updown-15m-1788761700", now) is MarketPhase.EXPIRED


def test_select_live_and_next_pairs_only():
    now = datetime(2026, 9, 7, 6, 34, tzinfo=timezone.utc)

    def pair(slug: str) -> MarketPair:
        return MarketPair(
            market_id=slug,
            condition_id=None,
            slug=slug,
            question="BTC Up or Down 15m",
            outcome_a="Up",
            outcome_b="Down",
            token_a=f"{slug}-up",
            token_b=f"{slug}-down",
            end_date=None,
        )

    selected = select_live_and_next_pairs(
        [
            pair("btc-updown-15m-1788761700"),
            pair("btc-updown-15m-1788762600"),
            pair("btc-updown-15m-1788763500"),
            pair("btc-updown-15m-1788764400"),
        ],
        now,
    )
    assert [p.slug for p in selected] == [
        "btc-updown-15m-1788762600",
        "btc-updown-15m-1788763500",
    ]


def test_current_gamma_event_shape_produces_binary_market_pair():
    now = datetime(2026, 9, 7, 5, 43, tzinfo=timezone.utc)
    discovery = MarketDiscovery("https://gamma-api.polymarket.com", "BTC Up or Down 15m")
    events = [
        {
            "id": "event-1",
            "slug": "btc-updown-15m-1788759000",
            "title": "BTC Up or Down 15m",
            "active": True,
            "closed": False,
            "endDate": "2026-09-07T06:00:00Z",
            "markets": [
                {
                    "id": "market-1",
                    "conditionId": "condition-1",
                    "slug": "btc-updown-15m-1788759000",
                    "question": "Bitcoin Up or Down - September 7, 1:30AM-1:45AM ET",
                    "active": True,
                    "closed": False,
                    "enableOrderBook": True,
                    "endDate": "2026-09-07T05:45:00Z",
                    "outcomes": '["Up", "Down"]',
                    "clobTokenIds": '["token-up", "token-down"]',
                }
            ],
        }
    ]

    pairs = discovery._pairs_from_events(events, now)

    assert len(pairs) == 1
    assert pairs[0].market_id == "market-1"
    assert pairs[0].token_a == "token-up"
    assert pairs[0].token_b == "token-down"
    assert pairs[0].outcome_a == "Up"
    assert pairs[0].outcome_b == "Down"


def test_date_only_gamma_end_date_does_not_expire_live_15m_market():
    now = datetime(2026, 9, 7, 5, 51, tzinfo=timezone.utc)
    discovery = MarketDiscovery("https://gamma-api.polymarket.com", "BTC Up or Down 15m")
    events = [
        {
            "id": "event-live",
            "slug": "btc-updown-15m-1788759900",
            "title": "BTC Up or Down 15m",
            "active": True,
            "closed": False,
            "endDate": "2026-09-07T00:00:00Z",
            "markets": [
                {
                    "id": "market-live",
                    "conditionId": "condition-live",
                    "slug": "btc-updown-15m-1788759900",
                    "question": "Bitcoin Up or Down - September 7, 1:45AM-2:00AM ET",
                    "active": True,
                    "closed": False,
                    "enableOrderBook": True,
                    "endDate": "2026-09-07T00:00:00Z",
                    "outcomes": '["Up", "Down"]',
                    "clobTokenIds": '["live-up", "live-down"]',
                }
            ],
        }
    ]

    pairs = discovery._pairs_from_events(events, now)

    assert len(pairs) == 1
    assert pairs[0].market_id == "market-live"
    assert pairs[0].end_date == "2026-09-07T06:00:00Z"


def test_live_recurring_market_survives_lagging_gamma_status_flags():
    now = datetime(2026, 9, 7, 6, 34, tzinfo=timezone.utc)
    discovery = MarketDiscovery("https://gamma-api.polymarket.com", "BTC Up or Down 15m")
    events = [
        {
            "id": "event-live-lag",
            "slug": "btc-updown-15m-1788762600",
            "title": "BTC Up or Down 15m",
            "markets": [
                {
                    "id": "market-live-lag",
                    "slug": "btc-updown-15m-1788762600",
                    "question": "Bitcoin Up or Down",
                    "active": False,
                    "closed": True,
                    "enableOrderBook": False,
                    "acceptingOrders": False,
                    "outcomes": '["Up", "Down"]',
                    "clobTokenIds": '["up-live", "down-live"]',
                }
            ],
        }
    ]

    pairs = discovery._pairs_from_events(events, now)
    assert len(pairs) == 1
    assert pairs[0].slug == "btc-updown-15m-1788762600"


def test_next_recurring_market_survives_preopen_gamma_flags():
    now = datetime(2026, 9, 7, 6, 34, tzinfo=timezone.utc)
    discovery = MarketDiscovery("https://gamma-api.polymarket.com", "BTC Up or Down 15m")
    events = [
        {
            "id": "event-next",
            "slug": "btc-updown-15m-1788763500",
            "title": "BTC Up or Down 15m",
            "markets": [
                {
                    "id": "market-next",
                    "slug": "btc-updown-15m-1788763500",
                    "question": "Bitcoin Up or Down",
                    "active": False,
                    "closed": False,
                    "enableOrderBook": False,
                    "acceptingOrders": False,
                    "outcomes": '["Up", "Down"]',
                    "clobTokenIds": '["up-next", "down-next"]',
                }
            ],
        }
    ]

    pairs = discovery._pairs_from_events(events, now)
    assert len(pairs) == 1
    assert pairs[0].slug == "btc-updown-15m-1788763500"


def test_slug_window_still_rejects_finished_15m_market():
    now = datetime(2026, 9, 7, 5, 51, tzinfo=timezone.utc)
    discovery = MarketDiscovery("https://gamma-api.polymarket.com", "BTC Up or Down 15m")
    events = [
        {
            "id": "event-finished",
            "slug": "btc-updown-15m-1788759000",
            "title": "BTC Up or Down 15m",
            "markets": [
                {
                    "id": "market-finished",
                    "slug": "btc-updown-15m-1788759000",
                    "question": "Bitcoin Up or Down - September 7, 1:30AM-1:45AM ET",
                    "active": True,
                    "closed": False,
                    "enableOrderBook": True,
                    "endDate": "2026-09-07T23:59:59Z",
                    "outcomes": '["Up", "Down"]',
                    "clobTokenIds": '["old-up", "old-down"]',
                }
            ],
        }
    ]

    assert discovery._pairs_from_events(events, now) == []
