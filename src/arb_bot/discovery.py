from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from .models import MarketPair


log = logging.getLogger(__name__)


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


class MarketDiscovery:
    def __init__(self, base_url: str, query: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.query = query

    async def discover(self) -> list[MarketPair]:
        params = {
            "q": self.query,
            "events_status": "active",
            "limit_per_type": 50,
            "keep_closed_markets": 0,
            "search_profiles": "false",
            "optimized": "true",
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(f"{self.base_url}/public-search", params=params)
            response.raise_for_status()
            payload = response.json()

        now = datetime.now(timezone.utc)
        pairs: list[MarketPair] = []
        for event in payload.get("events") or []:
            event_text = " ".join(str(event.get(k) or "") for k in ("title", "slug", "ticker", "description")).lower()
            if "bitcoin" not in event_text and "btc" not in event_text:
                continue
            for market in event.get("markets") or []:
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
                if parsed_end and parsed_end < now:
                    continue

                question = str(market.get("question") or event.get("title") or "")
                slug = str(market.get("slug") or event.get("slug") or market.get("id") or "unknown")
                market_text = f"{question} {slug}".lower()
                if "up" not in market_text or "down" not in market_text:
                    continue

                pairs.append(MarketPair(
                    market_id=str(market.get("id") or market.get("conditionId") or slug),
                    condition_id=market.get("conditionId"),
                    slug=slug,
                    question=question,
                    outcome_a=outcomes[0],
                    outcome_b=outcomes[1],
                    token_a=tokens[0],
                    token_b=tokens[1],
                    end_date=end_date,
                ))

        pairs.sort(key=lambda p: _parse_time(p.end_date) or datetime.max.replace(tzinfo=timezone.utc))
        log.info("Discovered %d active BTC Up/Down binary markets", len(pairs))
        return pairs
