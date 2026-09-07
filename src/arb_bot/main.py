from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv

from .config import Settings
from .dashboard import DashboardServer, DashboardState
from .diagnostics import LiveDiagnostics
from .diagnostics_v16 import log_dual_fok_diagnostics
from .discovery import MarketPhase, MarketDiscovery, asset_from_slug, market_phase, select_live_and_next_pairs
from .dual_fok_research import DualFOKResearchSuite
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
        "PHASE 1.7 MULTI-ASSET TERMINAL SHADOW MODE: all TAKER/MAKER/HEDGE/EV/DFOK/RFOK strategies are simulation-only; live order placement is not implemented."
    )
    log.info(
        "Market universe | assets=%s lookahead=%d intervals | dashboard=%s port=%d",
        settings.market_assets,
        settings.market_lookahead_intervals,
        settings.dashboard_enabled,
        settings.dashboard_port,
    )
    log.info(
        "Dual-FOK research | base_latency=%dms skews=%s sizes=%s edges=%s coverage=%s stability=%s surge_gate=%s leg_order=%s",
        settings.dual_fok_base_latency_ms,
        settings.dual_fok_skews_ms,
        settings.dual_fok_size_candidates,
        settings.dual_fok_edge_targets,
        settings.dual_fok_coverage_multiples,
        settings.dual_fok_stability_periods_ms,
        settings.dual_fok_use_surge_gate,
        settings.dual_fok_leg_order,
    )
    log.info(
        "EV historical control | EV_MIN_EXPECTED_PROFIT_USDC=%s independently of HEDGE_MIN_EXPECTED_PROFIT_USDC=%s",
        settings.ev_min_expected_profit_usdc,
        settings.hedge_min_expected_profit_usdc,
    )
    if not settings.split_sell_enabled:
        log.info("SPLITSELL remains disabled after strongly negative Phase 1.5 evidence")
    if settings.reverse_dual_fok_enabled:
        log.info("RFOK assumption | complete-set inventory is pre-positioned before detection; no hidden split latency is credited")
    await check_geoblock(settings)

    recorder = JsonlRecorder(settings.output_path)
    dashboard_state = DashboardState(settings)
    dashboard_state.bootstrap(settings.output_path)
    recorder.subscribe(dashboard_state.on_event)
    dashboard_server = DashboardServer(settings, dashboard_state.snapshot) if settings.dashboard_enabled else None
    if dashboard_server is not None:
        dashboard_server.start()

    discovery = MarketDiscovery(
        settings.gamma_url,
        settings.market_query,
        settings.market_assets,
        lookahead_intervals=settings.market_lookahead_intervals,
    )
    engine = ArbitrageEngine(settings)
    taker = ShadowExecutor(settings, recorder)
    research = MakerResearchSuite(settings, recorder)
    hedge = HedgeableResearchSuite(settings, recorder, research.regime)
    frontier = CorrectedEVFrontierSuite(settings, recorder, research.regime)
    split_sell = SplitSellResearchSuite(settings, recorder, research.regime) if settings.split_sell_enabled else None
    dual_fok = DualFOKResearchSuite(settings, recorder)
    edge_tracker = EdgeTracker(recorder, settings.edge_record_min_interval_ms)
    diagnostics = LiveDiagnostics(settings.diagnostic_interval_seconds)
    stream = PolymarketMarketStream(settings.websocket_url)
    started = time.monotonic()

    def process_strategy_timers() -> None:
        taker.process_due(engine.books)
        research.process_due(engine)
        hedge.process_due(engine)
        frontier.process_due(engine)
        dual_fok.process_due(engine)
        if split_sell is not None:
            split_sell.process_due(engine)

    def publish_dashboard() -> None:
        if not settings.dashboard_enabled:
            return
        dashboard_state.publish(
            engine,
            taker,
            edge_tracker,
            diagnostics,
            research=research,
            hedge=hedge,
            frontier=frontier,
            dual_fok=dual_fok,
            split_sell=split_sell,
        )

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

        research.on_market_update(engine, market_id)
        surge = research.regime.current(market_id)
        hedge.on_market_update(engine, market_id, surge)
        frontier.on_market_update(engine, market_id, surge)
        dual_fok.on_market_update(engine, market_id, surge)
        if split_sell is not None:
            split_sell.on_market_update(engine, market_id, surge)

        # Original pure taker benchmark remains as a historical control.
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
            log_dual_fok_diagnostics(dual_fok)

    async def dashboard_loop() -> None:
        while True:
            publish_dashboard()
            await asyncio.sleep(max(0.1, settings.dashboard_refresh_ms / 1000))

    timer_task = asyncio.create_task(timer_loop(), name="shadow-timers")
    diagnostic_task = asyncio.create_task(diagnostic_loop(), name="live-diagnostics")
    dashboard_task = asyncio.create_task(dashboard_loop(), name="dashboard-publisher") if settings.dashboard_enabled else None
    tasks = [timer_task, diagnostic_task] + ([dashboard_task] if dashboard_task is not None else [])

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
                log.warning("No configured 15m Up/Down markets found; retrying shortly")
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
                log.warning("No LIVE configured 15m market found; subscribing to NEXT markets until refresh")

            live_by_asset = {asset_from_slug(pair.slug): pair.slug for pair in live_pairs}
            next_by_asset = {asset_from_slug(pair.slug): pair.slug for pair in next_pairs}
            log.info(
                "Stream focus | LIVE=%s | NEXT=%s | pairs=%d tokens=%d | excluding %d distant/expired markets",
                live_by_asset or "none",
                next_by_asset or "none",
                len(stream_pairs),
                len(stream_pairs) * 2,
                max(0, len(pairs) - len(stream_pairs)),
            )

            process_strategy_timers()
            engine.set_markets(stream_pairs)
            publish_dashboard()
            token_ids = [token for pair in stream_pairs for token in (pair.token_a, pair.token_b)]
            refresh = settings.market_refresh_seconds
            if settings.run_seconds:
                remaining = settings.run_seconds - (time.monotonic() - started)
                refresh = max(0.1, min(refresh, remaining))
            try:
                await asyncio.wait_for(stream.run(token_ids, handle), timeout=refresh)
            except TimeoutError:
                process_strategy_timers()
                publish_dashboard()
                log.info("Refreshing active-market discovery")
            except asyncio.CancelledError:
                raise
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        publish_dashboard()
        recorder.unsubscribe(dashboard_state.on_event)
        if dashboard_server is not None:
            dashboard_server.stop()

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
    log_dual_fok_diagnostics(dual_fok)
    log.info("Finished Phase 1.7 shadow run | legacy TAKER=%+.4f pUSD", float(taker.total_pnl))
    for row in dual_fok.ranked_rows():
        log.info(
            "Finished %s | dir=%s eq=%+.4f placements=%d both=%d miss=%d neither=%d p_both=%.2f%% p_miss=%.2f%% EV/place=%+.5f life_avg=%.1fms life_p50=%.1fms",
            row["strategy"],
            row["direction"],
            float(row["equity"]),
            row["placements"],
            row["both_filled"],
            row["one_leg_miss"],
            row["neither_filled"],
            float(row["p_both"] * 100),
            float(row["p_miss"] * 100),
            float(row["ev_per_placement"]),
            float(row["avg_lifetime_ms"]),
            float(row["median_lifetime_ms"]),
        )


def cli() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli()
