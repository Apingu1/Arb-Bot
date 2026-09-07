from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv

from .config import Settings
from .diagnostics import LiveDiagnostics
from .discovery import MarketPhase, MarketDiscovery, market_phase, select_live_and_next_pairs
from .edge_tracker import EdgeTracker
from .maker_research import MakerResearchSuite
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
            "Polymarket geoblock: blocked=%s country=%s region=%s (shadow engines never submit live orders)",
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
    log.warning(
        "PHASE 1.3 QUEUE-AWARE SHADOW MODE: TAKER + MAKER/HYBRID 99/98/97/96 variants are simulation-only; live order placement is not implemented."
    )
    await check_geoblock(settings)

    recorder = JsonlRecorder(settings.output_path)
    discovery = MarketDiscovery(settings.gamma_url, settings.market_query)
    engine = ArbitrageEngine(settings)
    taker = ShadowExecutor(settings, recorder)
    research = MakerResearchSuite(settings, recorder)
    edge_tracker = EdgeTracker(recorder, settings.edge_record_min_interval_ms)
    diagnostics = LiveDiagnostics(settings.diagnostic_interval_seconds)
    stream = PolymarketMarketStream(settings.websocket_url)
    started = time.monotonic()

    def process_strategy_timers() -> None:
        taker.process_due(engine.books)
        research.process_due(engine)

    async def handle(message: dict) -> None:
        market_id = engine.apply_event(message)
        process_strategy_timers()
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

        # All maker/hybrid variants receive the same external market event but
        # maintain independent virtual queues, inventory and equity. They never
        # trade with or fill one another.
        research.on_market_update(engine, market_id)

        # Pure taker benchmark stays unchanged and only submits on the real LIVE
        # window when its fee/depth/risk-adjusted opportunity clears thresholds.
        if phase != MarketPhase.LIVE or not taker.can_submit(market_id):
            return
        opportunity = engine.evaluate(market_id)
        if opportunity:
            taker.submit(opportunity)

    async def timer_loop() -> None:
        while True:
            await asyncio.sleep(0.01)
            process_strategy_timers()

    async def diagnostic_loop() -> None:
        while True:
            await asyncio.sleep(settings.diagnostic_interval_seconds)
            process_strategy_timers()
            diagnostics.maybe_log(engine, taker, edge_tracker, research=research)

    timer_task = asyncio.create_task(timer_loop(), name="shadow-timers")
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
            stream_pairs = select_live_and_next_pairs(pairs, now_utc)
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

            # Give strategy engines one final chance to settle old-window state
            # before token books are pruned during rollover.
            process_strategy_timers()
            engine.set_markets(stream_pairs)
            token_ids = [token for pair in stream_pairs for token in (pair.token_a, pair.token_b)]
            refresh = settings.market_refresh_seconds
            if settings.run_seconds:
                remaining = settings.run_seconds - (time.monotonic() - started)
                refresh = max(0.1, min(refresh, remaining))
            try:
                await asyncio.wait_for(stream.run(token_ids, handle), timeout=refresh)
            except TimeoutError:
                process_strategy_timers()
                log.info("Refreshing active-market discovery")
            except asyncio.CancelledError:
                raise
    finally:
        timer_task.cancel()
        diagnostic_task.cancel()
        await asyncio.gather(timer_task, diagnostic_task, return_exceptions=True)

    process_strategy_timers()
    diagnostics.maybe_log(engine, taker, edge_tracker, research=research)
    log.info("Finished Phase 1.3 shadow run | TAKER=%+.4f pUSD", float(taker.total_pnl))
    for row in research.diagnostic_rows():
        log.info(
            "Finished %s | equity=%+.4f completed=%d inventory_exits=%d max_dd=%.4f",
            row["strategy"],
            float(row["equity"]),
            row["completed"],
            row["inventory_exits"],
            float(row["max_drawdown"]),
        )


def cli() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli()
