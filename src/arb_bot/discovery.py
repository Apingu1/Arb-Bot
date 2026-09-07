from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

import httpx

from .models import MarketPair


log = logging.getLogger(__name__)

BTC_15M_SLUG_PREFIX = "btc-updown-15m"
BTC_15M_INTERVAL_SECONDS = 15 * 60
BTC_15M_SLUG_RE = re.compile(r"^btc-updown-15m-(\d+)$")


class MarketPhase(str, Enum):
    LIVE = "LIVE"
    NEXT = "NEXT"
    FUTURE = "FUTURE"
    EXPIRED = "EXPIRED"
    OTHER = "OTHER"


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


def _token_outcomes(market: dict[str, Any]) -> tuple[list[str], list[str]]:
    tokens = _listish(market.get("clobTokenIds"))
    outcomes = _listish(market.get("outcomes"))
    if len(tokens) == 2 and len(outcomes) == 2:
        return tokens, outcomes

    nested = market.get("tokens")
    if isinstance(nested, list) and len(nested) == 2 and all(isinstance(item, dict) for item in nested):
        nested_tokens = [str(item.get("token_id") or item.get("tokenId") or item.get("id") or "") for item in nested]
        nested_outcomes = [str(item.get("outcome") or item.get("name") or "") for item in nested]
        if all(nested_tokens) and all(nested_outcomes):
            return nested_tokens, nested_outcomes

    return tokens, outcomes


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def btc_15m_window_from_slug(slug: str | None) -> tuple[datetime, datetime] | None:
    if not slug:
        return None
    match = BTC_15M_SLUG_RE.fullmatch(str(slug))
    if not match:
        return None
    start = datetime.fromtimestamp(int(match.group(1)), tz=timezone.utc)
    return start, start + timedelta(seconds=BTC_15M_INTERVAL_SECONDS)


def classify_btc_15m_slug(slug: str | None, now: datetime | None = None) -> MarketPhase:
    window = btc_15m_window_from_slug(slug)
    if not window:
        return MarketPhase.OTHER
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    start, end = window
    if now >= end:
        return MarketPhase.EXPIRED
    if start <= now < end:
        return MarketPhase.LIVE
    if start - timedelta(seconds=BTC_15M_INTERVAL_SECONDS) <= now < start:
        return MarketPhase.NEXT
    return MarketPhase.FUTURE


def btc_15m_candidate_slugs(
    now: datetime | None = None,
    *,
    lookback_intervals: int = 1,
    lookahead_intervals: int = 8,
) -> list[str]:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    anchor = (int(now.timestamp()) // BTC_15M_INTERVAL_SECONDS) * BTC_15M_INTERVAL_SECONDS
    return [
        f"{BTC_15M_SLUG_PREFIX}-{anchor + (offset * BTC_15M_INTERVAL_SECONDS)}"
        for offset in range(-lookback_intervals, lookahead_intervals + 1)
    ]


def select_live_and_next_pairs(pairs: list[MarketPair], now: datetime | None = None) -> list[MarketPair]:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    selected = [p for p in pairs if classify_btc_15m_slug(p.slug, now) in {MarketPhase.LIVE, MarketPhase.NEXT}]
    selected.sort(key=lambda p: btc_15m_window_from_slug(p.slug)[0] if btc_15m_window_from_slug(p.slug) else datetime.max.replace(tzinfo=timezone.utc))
    return selected


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
        rejected: Counter[str] = Counter()
        diagnostic_sample: dict[str, Any] | None = None

        for event in events:
            event_text = " ".join(str(event.get(k) or "") for k in ("title", "slug", "ticker", "description")).lower()
            if "bitcoin" not in event_text and "btc" not in event_text:
                rejected["non_btc_event"] += 1
                continue

            event_slug = str(event.get("slug") or "")
            event_window = btc_15m_window_from_slug(event_slug)
            event_phase = classify_btc_15m_slug(event_slug, now) if event_window else MarketPhase.OTHER
            raw_markets = event.get("markets")
            markets = raw_markets if isinstance(raw_markets, list) else []
            if not markets and (event.get("clobTokenIds") or event.get("tokens")):
                markets = [event]
            if not markets:
                rejected["event_without_markets"] += 1
                continue

            for market in markets:
                if not isinstance(market, dict):
                    rejected["invalid_market_shape"] += 1
                    continue

                if diagnostic_sample is None:
                    diagnostic_sample = {
                        "event_slug": event_slug,
                        "event_phase": event_phase.value,
                        "market_slug": market.get("slug"),
                        "active": market.get("active"),
                        "closed": market.get("closed"),
                        "enableOrderBook": market.get("enableOrderBook"),
                        "acceptingOrders": market.get("acceptingOrders"),
                        "clobTokenIds_present": bool(market.get("clobTokenIds")),
                        "outcomes": market.get("outcomes"),
                    }

                question = str(market.get("question") or event.get("title") or "")
                slug = str(market.get("slug") or event_slug or market.get("id") or "unknown")
                recurring_window = btc_15m_window_from_slug(slug) or event_window
                recurring_phase = classify_btc_15m_slug(slug, now) if btc_15m_window_from_slug(slug) else event_phase

                # For the deterministic BTC 15m recurring series, time encoded in
                # the slug is authoritative. Gamma lifecycle flags may lag around
                # the opening/closing boundary, so LIVE/NEXT recurring markets are
                # allowed through even when those generic flags are stale.
                if recurring_window:
                    if recurring_phase is MarketPhase.EXPIRED:
                        rejected["expired"] += 1
                        continue
                else:
                    if market.get("closed") is True:
                        rejected["closed"] += 1
                        continue
                    if market.get("active") is False:
                        rejected["inactive"] += 1
                        continue

                if market.get("enableOrderBook") is False and recurring_phase not in {MarketPhase.LIVE, MarketPhase.NEXT}:
                    rejected["orderbook_disabled"] += 1
                    continue

                tokens, outcomes = _token_outcomes(market)
                if len(tokens) != 2:
                    rejected["missing_tokens"] += 1
                    continue
                if len(outcomes) != 2:
                    rejected["missing_outcomes"] += 1
                    continue

                market_text = f"{question} {slug} {event.get('title') or ''}".lower()
                if "up" not in market_text or "down" not in market_text:
                    rejected["not_up_down"] += 1
                    continue

                if recurring_window:
                    _, exact_end = recurring_window
                    end_date = exact_end.isoformat().replace("+00:00", "Z")
                else:
                    end_date = market.get("endDateIso") or market.get("endDate") or event.get("endDate")
                    parsed_end = _parse_time(end_date)
                    if parsed_end and parsed_end <= now:
                        rejected["expired"] += 1
                        continue

                market_id = str(market.get("id") or market.get("conditionId") or slug)
                if market_id in seen_market_ids:
                    rejected["duplicate"] += 1
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

        pairs.sort(key=lambda p: btc_15m_window_from_slug(p.slug)[0] if btc_15m_window_from_slug(p.slug) else (_parse_time(p.end_date) or datetime.max.replace(tzinfo=timezone.utc)))
        if not pairs and rejected:
            log.warning("Discovery rejection summary: %s", dict(rejected))
            if diagnostic_sample:
                log.warning("Discovery sample market fields: %s", diagnostic_sample)
        return pairs

    async def discover(self) -> list[MarketPair]:
        now = datetime.now(timezone.utc)
        async with httpx.AsyncClient(timeout=15.0) as client:
            direct_events = await self._discover_recurring_events(client, now)
            search_events = await self._search_events(client)

        events_by_key: dict[str, dict[str, Any]] = {}
        for event in [*direct_events, *search_events]:
            key = str(event.get("id") or event.get("slug") or id(event))
            events_by_key[key] = event

        pairs = self._pairs_from_events(list(events_by_key.values()), now)
        phases = Counter(classify_btc_15m_slug(pair.slug, now).value for pair in pairs)
        log.info(
            "Discovered %d BTC Up/Down binary markets phases=%s (%d direct events, %d search events)",
            len(pairs),
            dict(phases),
            len(direct_events),
            len(search_events),
        )
        return pairs
