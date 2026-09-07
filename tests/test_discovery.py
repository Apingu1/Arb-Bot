from datetime import datetime, timezone

from arb_bot.discovery import MarketDiscovery, btc_15m_candidate_slugs


def test_btc_15m_candidate_slugs_are_aligned_to_current_window():
    now = datetime(2026, 9, 7, 5, 43, tzinfo=timezone.utc)
    slugs = btc_15m_candidate_slugs(now, lookback_intervals=1, lookahead_intervals=1)

    assert slugs == [
        "btc-updown-15m-1788758100",
        "btc-updown-15m-1788759000",
        "btc-updown-15m-1788759900",
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
