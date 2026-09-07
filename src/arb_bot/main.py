from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv

from .config import Settings
from .diagnostics import LiveDiagnostics
from .discovery import MarketPhase, MarketDiscovery, market_phase, select_stream_pairs
from .edge_tracker import EdgeTracker
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
        log.info(
            "Polymarket geoblock: blocked=%s country=%s region=%s (Phase 1 never submits live orders)",
            geo.get("blocked"),
            geo.get("country"),
            geo.get("region"),
        )
    except Exception as exc:
        log.warning("Could not read Polymarket geoblock endpoint: %s", exc)


async def run() -> None:
    load_dotenv()
    settings = Settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    log.warning("PHASE 1 SHADOW MODE: live order placement is intentionally not implemented.")
    await check_geoblock(settings)

    recorder = JsonlRecorder(settings.output_path)
    discovery = MarketDiscovery(settings.gamma_url, settings.market_query)
    engine = ArbitrageEngine(settings)
    shadow = ShadowExecutor(settings, recorder)
    edge_tracker = EdgeTracker(recorder, settings.edge_record_min_interval_ms)
    diagnostics = LiveDiagnostics(settings.diagnostic_interval_seconds)
    stream = PolymarketMarketStream(settings.websocket_url)
    started = time.monotonic()

    async def handle(message: dict) -> None:
        shadow.process_due(engine.books)
        market_id = engine.apply_event(message)
        diagnostics.observe(message, market_id)
        if not market_id:
            return

        pair = engine.pairs.get(market_id)
        if not pair:
            return

        phase = market_phase(pair, datetime.now(timezone.utc))
        edge_tracker.observe(
            engine,
            market_id,
            source_event=str(message.get("event_type") or "unknown"),
            exchange_timestamp=message.get("timestamp"),
        )

        # Shadow intents are deliberately restricted to the true current LIVE
        # window. NEXT is subscribed/recorded only so rollover starts warm.
        if phase != MarketPhase.LIVE or not shadow.can_submit(market_id):
            return

        opportunity = engine.evaluate(market_id)
        if opportunity:
            shadow.submit(opportunity)

    async def diagnostic_loop() -> None:
        while True:
            await asyncio.sleep(settings.diagnostic_interval_seconds)
            shadow.process_due(engine.books)
            diagnostics.maybe_log(engine, shadow, edge_tracker)

    diagnostic_task = asyncio.create_task(diagnostic_loop(), name="live-diagnostics")
    try:
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
                log.warning("No BTC Up/Down markets found; retrying shortly")
                await asyncio.sleep(min(settings.market_refresh_seconds, 15))
                continue

            now_utc = datetime.now(timezone.utc)
            stream_pairs = select_stream_pairs(pairs, now_utc)
            live_pairs = [pair for pair in stream_pairs if market_phase(pair, now_utc) == MarketPhase.LIVE]
            next_pairs = [pair for pair in stream_pairs if market_phase(pair, now_utc) == MarketPhase.NEXT]

            if not stream_pairs:
                log.warning("Discovery returned markets but none are LIVE/NEXT; retrying shortly")
                await asyncio.sleep(min(settings.market_refresh_seconds, 15))
                continue
            if not live_pairs:
                log.warning("No LIVE BTC 15m market found; subscribing to NEXT only until refresh")

            log.info(
                "Stream focus | LIVE=%s | NEXT=%s | excluding %d distant/expired markets",
                ",".join(pair.slug for pair in live_pairs) or "none",
                ",".join(pair.slug for pair in next_pairs) or "none",
                max(0, len(pairs) - len(stream_pairs)),
            )

            engine.set_markets(stream_pairs)
            token_ids = [token for pair in stream_pairs for token in (pair.token_a, pair.token_b)]
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
    finally:
        diagnostic_task.cancel()
        await asyncio.gather(diagnostic_task, return_exceptions=True)

    shadow.process_due(engine.books)
    diagnostics.maybe_log(engine, shadow, edge_tracker)
    log.info(
        "Finished shadow run: pnl=%+.4f completed=%d leg_misses=%d rejected=%d",
        float(shadow.total_pnl),
        shadow.completed,
        shadow.leg_misses,
        shadow.rejected,
    )


def cli() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli()
