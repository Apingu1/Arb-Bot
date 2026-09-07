from __future__ import annotations

import asyncio
import logging
import time

import httpx
from dotenv import load_dotenv

from .config import Settings
from .discovery import MarketDiscovery
from .polymarket_ws import PolymarketMarketStream
from .simulator import ShadowExecutor
from .storage import JsonlRecorder
from .strategy import ArbitrageEngine


log = logging.getLogger(__name__)


async def check_geoblock(settings: Settings) -> None:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(settings.geoblock_url)
            response.raise_for_status()
            geo = response.json()
        log.info("Polymarket geoblock: blocked=%s country=%s region=%s (Phase 1 never submits live orders)", geo.get("blocked"), geo.get("country"), geo.get("region"))
    except Exception as exc:
        log.warning("Could not read Polymarket geoblock endpoint: %s", exc)


async def run() -> None:
    load_dotenv()
    settings = Settings()
    logging.basicConfig(level=getattr(logging, settings.log_level, logging.INFO), format="%(asctime)s %(levelname)s %(name)s | %(message)s")
    log.warning("PHASE 1 SHADOW MODE: live order placement is intentionally not implemented.")
    await check_geoblock(settings)

    recorder = JsonlRecorder(settings.output_path)
    discovery = MarketDiscovery(settings.gamma_url, settings.market_query)
    engine = ArbitrageEngine(settings)
    shadow = ShadowExecutor(settings, recorder)
    stream = PolymarketMarketStream(settings.websocket_url)
    started = time.monotonic()

    async def handle(message: dict) -> None:
        shadow.process_due(engine.books)
        market_id = engine.apply_event(message)
        if not market_id or not shadow.can_submit(market_id):
            return
        opportunity = engine.evaluate(market_id)
        if opportunity:
            shadow.submit(opportunity)

    while True:
        if settings.run_seconds and time.monotonic() - started >= settings.run_seconds:
            break
        try:
            pairs = await discovery.discover()
        except Exception as exc:
            log.error("Market discovery failed: %s", exc)
            await asyncio.sleep(5)
            continue
        if not pairs:
            log.warning("No active BTC Up/Down markets found; retrying shortly")
            await asyncio.sleep(min(settings.market_refresh_seconds, 15))
            continue

        engine.set_markets(pairs)
        token_ids = [token for pair in pairs for token in (pair.token_a, pair.token_b)]
        refresh = settings.market_refresh_seconds
        if settings.run_seconds:
            remaining = settings.run_seconds - (time.monotonic() - started)
            refresh = max(0.1, min(refresh, remaining))
        try:
            await asyncio.wait_for(stream.run(token_ids, handle), timeout=refresh)
        except TimeoutError:
            shadow.process_due(engine.books)
            log.info("Refreshing active-market discovery")
        except asyncio.CancelledError:
            raise

    shadow.process_due(engine.books)
    log.info("Finished shadow run: pnl=%+.4f completed=%d leg_misses=%d rejected=%d", float(shadow.total_pnl), shadow.completed, shadow.leg_misses, shadow.rejected)


def cli() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli()
