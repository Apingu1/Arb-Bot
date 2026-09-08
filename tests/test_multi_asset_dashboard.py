from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from arb_bot.config import Settings
from arb_bot.dashboard import DashboardState, INDEX_HTML
from arb_bot.discovery import (
    MarketDiscovery,
    MarketPhase,
    asset_from_slug,
    market_phase,
    select_live_and_next_pairs,
    updown_15m_candidate_slugs,
    updown_15m_window_from_slug,
)
from arb_bot.models import MarketPair


def _settings(tmp_path, **overrides):
    values = dict(
        market_assets=("BTC", "ETH", "HYPE", "BNB", "DOGE", "XRP", "SOL"),
        dashboard_enabled=True,
        dashboard_state_path=str(tmp_path / "dashboard.json"),
        dashboard_event_limit=20,
        dashboard_major_loss_usdc=Decimal("0.25"),
        dashboard_major_win_usdc=Decimal("0.10"),
    )
    values.update(overrides)
    return Settings(**values)


def _pair(slug: str) -> MarketPair:
    return MarketPair(
        market_id=slug,
        condition_id=None,
        slug=slug,
        question=f"{asset_from_slug(slug)} Up or Down 15m",
        outcome_a="Up",
        outcome_b="Down",
        token_a=f"{slug}-up",
        token_b=f"{slug}-down",
        end_date=None,
    )


def test_generic_slug_helpers_cover_configured_assets():
    now = datetime(2026, 9, 7, 5, 43, tzinfo=timezone.utc)
    slugs = updown_15m_candidate_slugs("ETH", now, lookback_intervals=1, lookahead_intervals=1)
    assert slugs == [
        "eth-updown-15m-1788758100",
        "eth-updown-15m-1788759000",
        "eth-updown-15m-1788759900",
    ]
    assert asset_from_slug("doge-updown-15m-1788759000") == "DOGE"
    start, end = updown_15m_window_from_slug("sol-updown-15m-1788759000")
    assert start == datetime(2026, 9, 7, 5, 30, tzinfo=timezone.utc)
    assert end == datetime(2026, 9, 7, 5, 45, tzinfo=timezone.utc)


def test_live_next_selection_works_across_assets():
    now = datetime(2026, 9, 7, 6, 34, tzinfo=timezone.utc)
    pairs = [
        _pair("btc-updown-15m-1788762600"),
        _pair("btc-updown-15m-1788763500"),
        _pair("eth-updown-15m-1788762600"),
        _pair("eth-updown-15m-1788763500"),
        _pair("sol-updown-15m-1788764400"),
    ]
    selected = select_live_and_next_pairs(pairs, now)
    assert len(selected) == 4
    assert {asset_from_slug(pair.slug) for pair in selected} == {"BTC", "ETH"}
    assert sum(market_phase(pair, now) is MarketPhase.LIVE for pair in selected) == 2
    assert sum(market_phase(pair, now) is MarketPhase.NEXT for pair in selected) == 2


def test_discovery_accepts_configured_eth_and_rejects_unconfigured_sol():
    now = datetime(2026, 9, 7, 5, 43, tzinfo=timezone.utc)
    discovery = MarketDiscovery(
        "https://gamma-api.polymarket.com",
        "Up or Down 15m",
        assets=("BTC", "ETH"),
    )
    events = []
    for asset, market_id in (("eth", "eth-market"), ("sol", "sol-market")):
        slug = f"{asset}-updown-15m-1788759000"
        events.append(
            {
                "id": f"{asset}-event",
                "slug": slug,
                "title": f"{asset.upper()} Up or Down 15m",
                "markets": [
                    {
                        "id": market_id,
                        "slug": slug,
                        "question": f"{asset.upper()} Up or Down",
                        "active": True,
                        "closed": False,
                        "enableOrderBook": True,
                        "outcomes": '["Up", "Down"]',
                        "clobTokenIds": f'["{asset}-up", "{asset}-down"]',
                    }
                ],
            }
        )
    pairs = discovery._pairs_from_events(events, now)
    assert [pair.market_id for pair in pairs] == ["eth-market"]


def test_dashboard_reconstructs_aggregate_and_major_events(tmp_path):
    settings = _settings(tmp_path)
    state = DashboardState(settings)
    state.on_event(
        "strategy_equity",
        {
            "recorded_at": "2026-09-07T22:00:00Z",
            "strategy": "TAKER",
            "slug": "btc-updown-15m-1788818400",
            "status": "ONE_LEG_MISS",
            "action": "UNWIND_FILLED_LEG",
            "pnl_delta": Decimal("-1.25"),
        },
    )
    state.on_event(
        "strategy_equity",
        {
            "recorded_at": "2026-09-07T22:00:01Z",
            "strategy": "DFOK-TEST",
            "slug": "eth-updown-15m-1788818400",
            "status": "BOTH_FILLED",
            "action": "MERGE_COMPLETE_SET",
            "pnl_delta": Decimal("0.20"),
        },
    )

    engine = SimpleNamespace(pairs={}, books={}, settings=settings)
    taker = SimpleNamespace(
        pending_count=0,
        completed=0,
        leg_misses=0,
        rejected=0,
        total_pnl=Decimal("0"),
        empirical_risk=SimpleNamespace(attempts=0, miss_probability=Decimal("0")),
    )
    diagnostics = SimpleNamespace(total_messages=100)
    published = state.publish(engine, taker, None, diagnostics)

    assert published["aggregate_shadow_pnl"] == -1.05
    assert next(asset for asset in published["assets"] if asset["asset"] == "BTC")["pnl"] == -1.25
    assert next(asset for asset in published["assets"] if asset["asset"] == "ETH")["pnl"] == 0.2
    assert published["events"][0]["kind"] == "MAJOR_WIN"
    assert published["events"][1]["kind"] == "MAJOR_LOSS"
    assert json.loads((tmp_path / "dashboard.json").read_text(encoding="utf-8"))["aggregate_shadow_pnl"] == -1.05


def test_terminal_ui_contains_core_live_surfaces():
    assert "ARB//TERM" in INDEX_HTML
    assert "MODEL MATRIX" in INDEX_HTML
    assert "EVENT TAPE" in INDEX_HTML
    assert "LIVE / NEXT MARKET MATRIX" in INDEX_HTML
    assert "SHADOW NET" in INDEX_HTML.upper()
