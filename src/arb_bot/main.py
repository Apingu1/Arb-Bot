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
from .ev_frontier import SplitSellResearchSuite
from .ev_frontier_v151 import CorrectedEVFrontierSuite
from .hedgeable_research import HedgeableResearchSuite
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
        "PHASE 1.5.1 EV-EXPERIMENT-CORRECTION SHADOW MODE: TAKER + MAKER/HYBRID + HEDGE controls + corrected grace/size EV variants are simulation-only; live order placement is not implemented."
    )
    log.info(
        "EV research gate | edge-driven variants use EV_MIN_EXPECTED_PROFIT_USDC=%s independently of HEDGE_MIN_EXPECTED_PROFIT_USDC=%s",
        settings.ev_min_expected_profit_usdc,
        settings.hedge_min_expected_profit_usdc,
    )
    if not settings.split_sell_enabled:
        log.info("SPLITSELL disabled for Phase 1.5.1 corrected run after strongly negative Phase 1.5 evidence")
    await check_geoblock(settings)

    recorder = JsonlRecorder(settings.output_path)
    discovery = MarketDiscovery(settings.gamma_url, settings.market_query)
    engine = ArbitrageEngine(settings)
    taker = ShadowExecutor(settings, recorder)
    research = MakerResearchSuite(settings, recorder)
    hedge = HedgeableResearchSuite(settings, recorder, research.regime)
    frontier = CorrectedEVFrontierSuite(settings, recorder, research.regime)
    split_sell = SplitSellResearchSuite(settings, recorder, research.regime) if settings.split_sell_enabled else None
    edge_tracker = EdgeTracker(recorder, settings.edge_record_min_interval_ms)
    diagnostics = LiveDiagnostics(settings.diagnostic_interval_seconds)
    stream = PolymarketMarketStream(settings.websocket_url)
    started = time.monotonic()

    def process_strategy_timers() -> None:
        taker.process_due(engine.books)
        research.process_due(engine)
        hedge.process_due(engine)
        frontier.process_due(engine)
        if split_sell is not None:
            split_sell.process_due(engine)

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

        # Phase 1.3 controls update the shared SURGE tracker once.
        research.on_market_update(engine, market_id)
        surge = research.regime.current(market_id)

        # Later research families consume the exact same external event/regime
        # but maintain independent virtual queues, inventory and equity.
        hedge.on_market_update(engine, market_id, surge)
        frontier.on_market_update(engine, market_id, surge)
        if split_sell is not None:
            split_sell.on_market_update(engine, market_id, surge)

        # Pure taker benchmark remains unchanged.
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
            diagnostics.maybe_log(
                engine,
                taker,
                edge_tracker,
                research=research,
                hedge=hedge,
                frontier=frontier,
                split_sell=split_sell,
            )

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
    diagnostics.maybe_log(
        engine,
        taker,
        edge_tracker,
        research=research,
        hedge=hedge,
        frontier=frontier,
        split_sell=split_sell,
    )
    log.info("Finished Phase 1.5.1 shadow run | TAKER=%+.4f pUSD", float(taker.total_pnl))
    for row in research.diagnostic_rows():
        log.info(
            "Finished %s | equity=%+.4f completed=%d inventory_exits=%d max_dd=%.4f",
            row["strategy"],
            float(row["equity"]),
            row["completed"],
            row["inventory_exits"],
            float(row["max_drawdown"]),
        )
    for row in hedge.diagnostic_rows():
        log.info(
            "Finished %s | equity=%+.4f maker_fills=%d hedge=%d/%d recover_complete=%d recover_unwind=%d max_dd=%.4f",
            row["strategy"],
            float(row["equity"]),
            row["maker_fills"],
            row["hedge_successes"],
            row["hedge_attempts"],
            row["recovery_completions"],
            row["recovery_unwinds"],
            float(row["max_drawdown"]),
        )
    ranked = frontier.ranked_rows()
    if not ranked:
        log.info("EV FRONTIER FINAL | INSUFFICIENT DATA: no variant has a realized maker fill or ghost fill")
    for row in ranked:
        log.info(
            "Finished %s | eq=%+.4f p_fill=%.2f%% p_hedge=%.2f%% model_EV=%+.5f realized_EV=%+.5f ghost=%d/%d ghost_avg=%+.5f ghost_median=%+.5f ghost_best=%+.5f ghost_worst=%+.5f",
            row["strategy"],
            float(row["equity"]),
            float(row["p_fill"] * 100),
            float(row["p_hedge_given_fill"] * 100),
            float(row["modeled_ev_per_placement"]),
            float(row["realized_ev_per_placement"]),
            row["ghost_filled"],
            row["ghost_created"],
            float(row["ghost_avg_best_recovery_pnl"]),
            float(row["ghost_median_best_recovery_pnl"]),
            float(row["ghost_best_recovery_pnl"]),
            float(row["ghost_worst_recovery_pnl"]),
        )
    if split_sell is not None:
        for row in split_sell.diagnostic_rows():
            log.info(
                "Finished %s | eq=%+.4f p_fill=%.2f%% p_complete=%.2f%% EV/placement=%+.5f",
                row["strategy"],
                float(row["equity"]),
                float(row["p_fill"] * 100),
                float(row["p_complete_given_fill"] * 100),
                float(row["ev_per_placement"]),
            )


def cli() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli()