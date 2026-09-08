from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from statistics import median
from typing import Any

from .config import Settings
from .discovery import MarketPhase, asset_from_slug, market_phase
from .fees import taker_fee
from .models import ExecutionQuote, MarketPair
from .storage import JsonlRecorder
from .strategy import ArbitrageEngine


log = logging.getLogger(__name__)
ZERO = Decimal("0")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _avg(values: list[Decimal]) -> Decimal:
    return sum(values, ZERO) / Decimal(len(values)) if values else ZERO


def _size_code(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _quote_payload(quote: ExecutionQuote) -> dict[str, Any]:
    return {
        "shares": quote.shares,
        "notional": quote.notional,
        "average_price": quote.average_price,
        "marginal_price": quote.marginal_price,
        "segments": [{"price": item.price, "shares": item.shares} for item in quote.segments],
    }


@dataclass(slots=True)
class AtomicWindow:
    slug: str
    started_at: float
    started_at_utc: str
    capture_edge: Decimal
    peak_edge: Decimal
    capture_pnl: Decimal
    pair_price: Decimal


class IdealAtomicVariant:
    """Perfect-snapshot control: both complementary legs fill simultaneously.

    This is deliberately NOT an executable strategy and never emits
    ``strategy_equity``. Its P&L answers a research question only: if the two
    legs at one observed order-book snapshot could be captured atomically with
    zero arrival skew, how much complete-set edge existed after protocol fees?
    """

    def __init__(self, settings: Settings, recorder: JsonlRecorder, *, shares: Decimal, direction: str) -> None:
        self.settings = settings
        self.recorder = recorder
        self.shares = shares
        self.direction = direction
        prefix = "ATOMIC-BUY" if direction == "BUY_PAIR" else "ATOMIC-SELL"
        self.strategy = f"{prefix}-S{_size_code(shares)}"
        self.active: dict[str, AtomicWindow] = {}
        self.captures = 0
        self.benchmark_pnl = ZERO
        self.edges: list[Decimal] = []
        self.lifetimes_ms: list[Decimal] = []
        self.edge_band_counts: dict[Decimal, int] = {band: 0 for band in settings.atomic_edge_bands}
        self.asset_captures: dict[str, int] = {}
        self.asset_pnl: dict[str, Decimal] = {}
        self.book_rejects = 0

    def _book_fresh(self, updated: float, now: float) -> bool:
        return updated > 0 and now - updated <= self.settings.atomic_max_book_age_ms / 1000

    def _snapshot(self, engine: ArbitrageEngine, market_id: str, now: float) -> dict[str, Any] | None:
        pair = engine.pairs.get(market_id)
        if pair is None or market_phase(pair) != MarketPhase.LIVE:
            return None
        a = engine.books.get(pair.token_a)
        b = engine.books.get(pair.token_b)
        if not a or not b or not a.ready or not b.ready:
            self.book_rejects += 1
            return None
        if not self._book_fresh(a.updated_monotonic, now) or not self._book_fresh(b.updated_monotonic, now):
            self.book_rejects += 1
            return None

        if self.direction == "BUY_PAIR":
            quote_a = a.quote_buy(self.shares)
            quote_b = b.quote_buy(self.shares)
            if quote_a is None or quote_b is None:
                return None
            fee_a = taker_fee(quote_a.segments, self.settings.crypto_taker_fee_rate)
            fee_b = taker_fee(quote_b.segments, self.settings.crypto_taker_fee_rate)
            pnl = self.shares - quote_a.notional - quote_b.notional - fee_a - fee_b
        else:
            quote_a = a.quote_sell(self.shares)
            quote_b = b.quote_sell(self.shares)
            if quote_a is None or quote_b is None:
                return None
            fee_a = taker_fee(quote_a.segments, self.settings.crypto_taker_fee_rate)
            fee_b = taker_fee(quote_b.segments, self.settings.crypto_taker_fee_rate)
            pnl = quote_a.notional + quote_b.notional - self.shares - fee_a - fee_b

        edge = pnl / self.shares
        pair_price = (quote_a.notional + quote_b.notional) / self.shares
        return {
            "pair": pair,
            "quote_a": quote_a,
            "quote_b": quote_b,
            "fee_a": fee_a,
            "fee_b": fee_b,
            "pnl": pnl,
            "edge": edge,
            "pair_price": pair_price,
        }

    def on_market_update(self, engine: ArbitrageEngine, market_id: str) -> None:
        now = time.monotonic()
        metrics = self._snapshot(engine, market_id, now)
        if metrics is None or metrics["edge"] < self.settings.atomic_min_net_edge_per_share:
            self._close(market_id, now, "NO_LONGER_POSITIVE")
            return

        existing = self.active.get(market_id)
        if existing is not None:
            existing.peak_edge = max(existing.peak_edge, metrics["edge"])
            return

        pair: MarketPair = metrics["pair"]
        window = AtomicWindow(
            slug=pair.slug,
            started_at=now,
            started_at_utc=_utc_now(),
            capture_edge=metrics["edge"],
            peak_edge=metrics["edge"],
            capture_pnl=metrics["pnl"],
            pair_price=metrics["pair_price"],
        )
        self.active[market_id] = window
        self.captures += 1
        self.benchmark_pnl += metrics["pnl"]
        self.edges.append(metrics["edge"])
        asset = asset_from_slug(pair.slug) or "UNKNOWN"
        self.asset_captures[asset] = self.asset_captures.get(asset, 0) + 1
        self.asset_pnl[asset] = self.asset_pnl.get(asset, ZERO) + metrics["pnl"]
        for band in self.edge_band_counts:
            if metrics["edge"] >= band:
                self.edge_band_counts[band] += 1

        self.recorder.write(
            "atomic_benchmark_capture",
            {
                "strategy": self.strategy,
                "mode": "IDEAL_ATOMIC_BENCHMARK",
                "direction": self.direction,
                "benchmark_only": True,
                "captured_at": window.started_at_utc,
                "finalized_at": window.started_at_utc,
                "market_id": pair.market_id,
                "slug": pair.slug,
                "asset": asset,
                "status": "CAPTURED",
                "action": "INSTANT_SIMULTANEOUS_COMPLETE_SET",
                "shares": self.shares,
                "detected_pair_price": metrics["pair_price"],
                "detected_edge_per_share": metrics["edge"],
                "taker_fee_paid": metrics["fee_a"] + metrics["fee_b"],
                "initial_execution": {
                    "leg_a": _quote_payload(metrics["quote_a"]),
                    "leg_b": _quote_payload(metrics["quote_b"]),
                },
                # Kept for arb-report compatibility; it is explicitly benchmark-only.
                "realized_pnl": metrics["pnl"],
                "equity_after": self.benchmark_pnl,
                "prepositioned_complete_set_inventory": self.direction == "SELL_PAIR",
            },
        )

    def _close(self, market_id: str, now: float, reason: str) -> None:
        window = self.active.pop(market_id, None)
        if window is None:
            return
        lifetime = Decimal(str(max(0.0, (now - window.started_at) * 1000)))
        self.lifetimes_ms.append(lifetime)
        self.recorder.write(
            "atomic_benchmark_lifetime",
            {
                "strategy": self.strategy,
                "direction": self.direction,
                "benchmark_only": True,
                "slug": window.slug,
                "asset": asset_from_slug(window.slug) or "UNKNOWN",
                "started_at": window.started_at_utc,
                "ended_at": _utc_now(),
                "lifetime_ms": lifetime,
                "capture_edge_per_share": window.capture_edge,
                "peak_edge_per_share": window.peak_edge,
                "capture_pnl": window.capture_pnl,
                "pair_price": window.pair_price,
                "end_reason": reason,
            },
        )

    def process_due(self, engine: ArbitrageEngine) -> None:
        now = time.monotonic()
        for market_id in list(self.active):
            pair = engine.pairs.get(market_id)
            if pair is None or market_phase(pair) != MarketPhase.LIVE:
                self._close(market_id, now, "WINDOW_ROLLOVER")

    def diagnostic_row(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "mode": "IDEAL_ATOMIC_BENCHMARK",
            "direction": self.direction,
            "benchmark": True,
            "shares": self.shares,
            "captures": self.captures,
            "placements": self.captures,
            "completed": self.captures,
            "wins": self.captures,
            "losses": 0,
            "benchmark_pnl": self.benchmark_pnl,
            "equity": self.benchmark_pnl,
            "avg_edge": _avg(self.edges),
            "avg_lifetime_ms": _avg(self.lifetimes_ms),
            "median_lifetime_ms": Decimal(str(median(self.lifetimes_ms))) if self.lifetimes_ms else ZERO,
            "lifetime_samples": len(self.lifetimes_ms),
            "active_windows": len(self.active),
            "edge_band_counts": {str(k): v for k, v in self.edge_band_counts.items()},
            "book_rejects": self.book_rejects,
        }

    def asset_rows(self) -> list[dict[str, Any]]:
        return [
            {
                "asset": asset,
                "strategy": self.strategy,
                "family": "ATOMIC",
                "benchmark": True,
                "captures": self.asset_captures.get(asset, 0),
                "pnl": self.asset_pnl.get(asset, ZERO),
            }
            for asset in sorted(set(self.asset_captures) | set(self.asset_pnl))
        ]


class IdealAtomicBenchmarkSuite:
    def __init__(self, settings: Settings, recorder: JsonlRecorder) -> None:
        self.settings = settings
        self.recorder = recorder
        self.variants: list[IdealAtomicVariant] = []
        if not settings.atomic_benchmark_enabled:
            return
        for shares in settings.atomic_sizes:
            self.variants.append(IdealAtomicVariant(settings, recorder, shares=shares, direction="BUY_PAIR"))
            if settings.atomic_reverse_enabled:
                self.variants.append(IdealAtomicVariant(settings, recorder, shares=shares, direction="SELL_PAIR"))

    def on_market_update(self, engine: ArbitrageEngine, market_id: str) -> None:
        for variant in self.variants:
            variant.on_market_update(engine, market_id)

    def process_due(self, engine: ArbitrageEngine) -> None:
        for variant in self.variants:
            variant.process_due(engine)

    def diagnostic_rows(self) -> list[dict[str, Any]]:
        return [variant.diagnostic_row() for variant in self.variants]

    def asset_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for variant in self.variants:
            rows.extend(variant.asset_rows())
        return rows


def log_atomic_diagnostics(suite: IdealAtomicBenchmarkSuite) -> None:
    for row in suite.diagnostic_rows():
        if not row["captures"] and not row["active_windows"]:
            continue
        log.info(
            "%s | IDEAL ONLY captures=%d active=%d pnl=%+.5f avg_edge=%+.5f life(avg/p50)=%.1f/%.1fms bands=%s",
            row["strategy"],
            row["captures"],
            row["active_windows"],
            float(row["benchmark_pnl"]),
            float(row["avg_edge"]),
            float(row["avg_lifetime_ms"]),
            float(row["median_lifetime_ms"]),
            row["edge_band_counts"],
        )
