from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from .models import MarketPair


log = logging.getLogger(__name__)

BTC_15M_SLUG_PREFIX = "btc-updown-15m"
BTC_15M_INTERVAL_SECONDS = 15 * 60


def _listish(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        value = value.strip()
        if value.startswith("["):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    return [str(v) for v in parsed]
            except json.JSONDecodeError:
                pass
    return []


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def btc_15m_candidate_slugs(
    now: datetime | None = None,
    *,
    lookback_intervals: int = 1,
    lookahead_intervals: int = 8,
) -> list[str]:
    """Return recurring BTC 15-minute event slugs around the current window.

    Polymarket's recurring BTC 15m events use the UTC Unix timestamp of the
    interval start in the slug, for example ``btc-updown-15m-1788753600``.
    Looking slightly behind and ahead makes discovery robust during rollovers
    and lets the websocket subscribe before the next interval begins.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    anchor = (int(now.timestamp()) // BTC_15M_INTERVAL_SECONDS) * BTC_15M_INTERVAL_SECONDS
    return [
        f"{BTC_15M_SLUG_PREFIX}-{anchor + (offset * BTC_15M_INTERVAL_SECONDS)}"
        for offset in range(-lookback_intervals, lookahead_intervals + 1)
    ]


class MarketDiscovery:
    def __init__(self, base_url: str, query: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.query = query

    async def _fetch_event_by_slug(self, client: httpx.AsyncClient, slug: str) -> dict[str, Any] | None:
        try:
            response = await client.get(f"{self.base_url}/events/slug/{slug}")
            if response.status_code == 404:
                return None
            response.raise_for_status()
            payload = response.json()
            return payload if isinstance(payload, dict) else None
        except httpx.HTTPError as exc:
            log.debug("Direct event lookup failed for %s: %s", slug, exc)
            return None

    async def _discover_recurring_events(self, client: httpx.AsyncClient, now: datetime) -> list[dict[str, Any]]:
        slugs = btc_15m_candidate_slugs(now)
        results = await asyncio.gather(*(self._fetch_event_by_slug(client, slug) for slug in slugs))
        events = [event for event in results if event]
        log.info("Direct BTC 15m slug discovery found %d/%d candidate events", len(events), len(slugs))
        return events

    async def _search_events(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        # Keep search as a fallback/secondary source. The recurring series is
        # titled "BTC Up or Down 15m", which is more reliable than the older
        # generic "Bitcoin Up or Down" query, but direct slug lookup remains
        # the primary discovery method.
        queries = []
        for query in ("BTC Up or Down 15m", self.query):
            if query and query not in queries:
                queries.append(query)

        events: list[dict[str, Any]] = []
        for query in queries:
            params = {
                "q": query,
                "events_status": "active",
                "limit_per_type": 50,
                "keep_closed_markets": 0,
                "search_profiles": "false",
                "optimized": "true",
            }
            try:
                response = await client.get(f"{self.base_url}/public-search", params=params)
                response.raise_for_status()
                payload = response.json()
            except httpx.HTTPError as exc:
                log.debug("Gamma public-search failed for %r: %s", query, exc)
                continue

            found = payload.get("events") if isinstance(payload, dict) else None
            if isinstance(found, list):
                events.extend(event for event in found if isinstance(event, dict))

        return events

    def _pairs_from_events(self, events: list[dict[str, Any]], now: datetime) -> list[MarketPair]:
        pairs: list[MarketPair] = []
        seen_market_ids: set[str] = set()

        for event in events:
            event_text = " ".join(
                str(event.get(k) or "") for k in ("title", "slug", "ticker", "description")
            ).lower()
            if "bitcoin" not in event_text and "btc" not in event_text:
                continue

            for market in event.get("markets") or []:
                if not isinstance(market, dict):
                    continue
                if market.get("closed") is True or market.get("active") is False:
                    continue
                if market.get("enableOrderBook") is False:
                    continue

                tokens = _listish(market.get("clobTokenIds"))
                outcomes = _listish(market.get("outcomes"))
                if len(tokens) != 2 or len(outcomes) != 2:
                    continue

                end_date = market.get("endDateIso") or market.get("endDate") or event.get("endDate")
                parsed_end = _parse_time(end_date)
                if parsed_end and parsed_end <= now:
                    continue

                question = str(market.get("question") or event.get("title") or "")
                slug = str(market.get("slug") or event.get("slug") or market.get("id") or "unknown")
                market_text = f"{question} {slug} {event.get('title') or ''}".lower()
                if "up" not in market_text or "down" not in market_text:
                    continue

                market_id = str(market.get("id") or market.get("conditionId") or slug)
                if market_id in seen_market_ids:
                    continue
                seen_market_ids.add(market_id)

                pairs.append(
                    MarketPair(
                        market_id=market_id,
                        condition_id=market.get("conditionId"),
                        slug=slug,
                        question=question,
                        outcome_a=outcomes[0],
                        outcome_b=outcomes[1],
                        token_a=tokens[0],
                        token_b=tokens[1],
                        end_date=end_date,
                    )
                )

        pairs.sort(key=lambda p: _parse_time(p.end_date) or datetime.max.replace(tzinfo=timezone.utc))
        return pairs

    async def discover(self) -> list[MarketPair]:
        now = datetime.now(timezone.utc)
        async with httpx.AsyncClient(timeout=15.0) as client:
            direct_events = await self._discover_recurring_events(client, now)
            search_events = await self._search_events(client)

        # Deduplicate the same event when it is found by both mechanisms.
        events_by_key: dict[str, dict[str, Any]] = {}
        for event in [*direct_events, *search_events]:
            key = str(event.get("id") or event.get("slug") or id(event))
            events_by_key[key] = event

        pairs = self._pairs_from_events(list(events_by_key.values()), now)
        log.info(
            "Discovered %d active BTC Up/Down binary markets (%d direct events, %d search events)",
            len(pairs),
            len(direct_events),
            len(search_events),
        )
        return pairs
