from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

import websockets


log = logging.getLogger(__name__)
EventHandler = Callable[[dict], Awaitable[None]]


class PolymarketMarketStream:
    def __init__(self, url: str) -> None:
        self.url = url

    async def run(self, token_ids: list[str], handler: EventHandler) -> None:
        backoff = 1
        while True:
            try:
                async with websockets.connect(self.url, ping_interval=None, close_timeout=3, max_queue=4096) as ws:
                    await ws.send(json.dumps({"assets_ids": token_ids, "type": "market", "custom_feature_enabled": True}))
                    log.info("Subscribed to %d Polymarket outcome tokens", len(token_ids))
                    heartbeat = asyncio.create_task(self._heartbeat(ws))
                    backoff = 1
                    try:
                        async for raw in ws:
                            if raw == "PONG":
                                continue
                            payload = json.loads(raw)
                            messages = payload if isinstance(payload, list) else [payload]
                            for message in messages:
                                if isinstance(message, dict):
                                    await handler(message)
                    finally:
                        heartbeat.cancel()
                        await asyncio.gather(heartbeat, return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Market WebSocket disconnected: %s; reconnecting in %ss", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 15)

    @staticmethod
    async def _heartbeat(ws) -> None:
        while True:
            await asyncio.sleep(10)
            await ws.send("PING")
