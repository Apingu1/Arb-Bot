from __future__ import annotations

import time

from .strategy import ArbitrageEngine


class ArbitrageEngineV186(ArbitrageEngine):
    """Arbitrage engine with lightweight callback/book-update timestamps.

    The timestamps are in-process monotonic clocks only. They let Phase 1.8.6
    separate local parsing/book-update time from later strategy/scheduler delay.
    """

    def __init__(self, settings) -> None:
        super().__init__(settings)
        self.v186_event_timing: dict[str, dict[str, float]] = {}

    def apply_event(self, message: dict):
        started = time.monotonic()
        market_id = super().apply_event(message)
        completed = time.monotonic()
        if market_id:
            self.v186_event_timing[market_id] = {
                "callback_started": started,
                "book_updated": completed,
            }
        return market_id
