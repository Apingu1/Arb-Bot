from __future__ import annotations

import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from .discovery import MarketPhase, asset_from_slug, market_phase
from .fees import taker_fee


ZERO = Decimal("0")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(slots=True)
class FunnelContextV187:
    market_id: str
    slug: str
    asset: str
    observed_at: str
    raw_before: dict[str, Any]
    bfok_before: dict[str, int]
    bfok_reason: dict[str, str]
    pfok_before: dict[str, int] = field(default_factory=dict)
    pfok_reason: dict[str, str] = field(default_factory=dict)
    raw_attempted: bool = False
    raw_status: str | None = None
    raw_pnl: Decimal = ZERO
    raw_edge: Decimal | None = None
    raw_win: bool = False
    protected: dict[str, dict[str, Any]] = field(default_factory=dict)


class OpportunityFunnelV187:
    """Compact opportunity funnel plus RAW-win/protected-model attribution.

    Counts are accumulated in memory and written as compact rollups. A separate
    sparse event is emitted only when BFOK-RAW completes a profitable pair. It
    records whether each protected BFOK/PFOK model entered on that same market
    update, or the specific detection-stage reason it did not.

    This is an entry-attribution diagnostic, not a counterfactual guarantee that
    a protected model would have filled at its later modeled arrival time.
    """

    EDGE_THRESHOLDS = (
        ("EDGE_GE_M050", Decimal("-0.050")),
        ("EDGE_GE_M020", Decimal("-0.020")),
        ("EDGE_GE_0", Decimal("0")),
        ("EDGE_GE_001", Decimal("0.001")),
        ("EDGE_GE_003", Decimal("0.003")),
        ("EDGE_GE_005", Decimal("0.005")),
    )

    def __init__(self, settings, recorder) -> None:
        self.settings = settings
        self.recorder = recorder
        self.enabled = bool(getattr(settings, "v187_funnel_enabled", True))
        self.rollup_seconds = max(1, int(getattr(settings, "v187_funnel_rollup_seconds", 5)))
        self.counts: Counter[str] = Counter()
        self.model_reasons: dict[str, Counter[str]] = defaultdict(Counter)
        self.model_entries: Counter[str] = Counter()
        self.raw_win_model_reasons: dict[str, Counter[str]] = defaultdict(Counter)
        self.raw_win_model_entries: Counter[str] = Counter()
        self._active: dict[str, FunnelContextV187] = {}
        self._last_rollup = time.monotonic()

    @staticmethod
    def _depth_at_limit(levels: dict[Decimal, Decimal], limit: Decimal) -> Decimal:
        return sum((qty for px, qty in levels.items() if px <= limit), ZERO)

    def _pair_metrics(self, engine, pair, shares: Decimal, *, max_age_ms: int | None) -> dict[str, Any] | None:
        now = time.monotonic()
        a = engine.books.get(pair.token_a)
        b = engine.books.get(pair.token_b)
        if a is None or b is None or not a.ready or not b.ready:
            return None
        if max_age_ms is not None:
            if (
                a.updated_monotonic <= 0
                or b.updated_monotonic <= 0
                or (now - a.updated_monotonic) * 1000 > max_age_ms
                or (now - b.updated_monotonic) * 1000 > max_age_ms
            ):
                return {"stale": True}
        qa = a.quote_buy(shares)
        qb = b.quote_buy(shares)
        if qa is None or qb is None:
            return {"no_full_pair": True}
        fee_a = taker_fee(qa.segments, self.settings.crypto_taker_fee_rate)
        fee_b = taker_fee(qb.segments, self.settings.crypto_taker_fee_rate)
        pnl = shares - qa.notional - qb.notional - fee_a - fee_b
        cov_a = self._depth_at_limit(a.asks, qa.marginal_price) / shares
        cov_b = self._depth_at_limit(b.asks, qb.marginal_price) / shares
        return {
            "edge": pnl / shares,
            "pnl": pnl,
            "coverage": min(cov_a, cov_b),
            "coverage_a": cov_a,
            "coverage_b": cov_b,
        }

    def _classify_bfok(self, engine, market_id: str, surge, variant) -> str:
        pair = engine.pairs.get(market_id)
        if pair is None or market_phase(pair) != MarketPhase.LIVE:
            return "NOT_LIVE"
        now = time.monotonic()
        if market_id in variant.pending:
            return "PENDING"
        if now < variant.cooldown_until.get(market_id, 0.0):
            return "COOLDOWN"
        if variant.settings.v187_use_surge_gate and surge is not None and surge.active:
            return "SURGE"

        if variant.fixed_size is None:
            accepted = False
            saw_full = False
            saw_edge = False
            saw_cov = False
            asset = asset_from_slug(pair.slug) or "UNKNOWN"
            for shares in variant.settings.v187_ev_sizes:
                metrics = self._pair_metrics(engine, pair, shares, max_age_ms=variant.settings.v187_max_book_age_ms)
                if not metrics:
                    continue
                if metrics.get("stale"):
                    return "STALE_BOOK"
                if metrics.get("no_full_pair"):
                    continue
                saw_full = True
                if metrics["edge"] < variant.settings.v187_detection_min_edge_per_share:
                    continue
                saw_edge = True
                if metrics["coverage"] < variant.settings.v187_detection_coverage_multiple:
                    continue
                saw_cov = True
                est = variant.risk_book.estimate(
                    asset=asset,
                    shares=shares,
                    detected_edge=metrics["edge"],
                    detected_pnl=metrics["pnl"],
                )
                if est["expected_pnl"] >= variant.settings.v187_ev_min_expected_pnl:
                    accepted = True
                    break
            if accepted:
                return "QUALIFIED"
            if not saw_full:
                return "NO_FULL_SIZE_PAIR"
            if not saw_edge:
                return "EDGE"
            if not saw_cov:
                return "COVERAGE"
            return "EV_GATE"

        metrics = self._pair_metrics(engine, pair, variant.fixed_size, max_age_ms=variant.settings.v187_max_book_age_ms)
        if not metrics:
            return "BOOK_NOT_READY"
        if metrics.get("stale"):
            return "STALE_BOOK"
        if metrics.get("no_full_pair"):
            return "NO_FULL_SIZE_PAIR"
        if metrics["edge"] < variant.settings.v187_detection_min_edge_per_share:
            return "EDGE"
        if metrics["coverage"] < variant.settings.v187_detection_coverage_multiple:
            return "COVERAGE"
        return "QUALIFIED"

    def _classify_pfok(self, engine, market_id: str, surge, variant) -> str:
        pair = engine.pairs.get(market_id)
        if pair is None or market_phase(pair) != MarketPhase.LIVE:
            return "NOT_LIVE"
        now = time.monotonic()
        if market_id in variant.pending:
            return "PENDING"
        if now < variant.cooldown_until.get(market_id, 0.0):
            return "COOLDOWN"
        if variant.settings.v185_use_surge_gate and surge is not None and surge.active:
            return "SURGE"

        saw_full = False
        saw_edge = False
        for shares in sorted(variant.settings.v185_profit_sizes, reverse=True):
            metrics = self._pair_metrics(engine, pair, shares, max_age_ms=variant.settings.v185_max_book_age_ms)
            if not metrics:
                continue
            if metrics.get("stale"):
                return "STALE_BOOK"
            if metrics.get("no_full_pair"):
                continue
            saw_full = True
            if metrics["edge"] < variant.settings.v185_detection_min_edge_per_share:
                continue
            saw_edge = True
            if metrics["coverage"] < variant.settings.v185_detection_coverage_multiple:
                continue
            return "QUALIFIED"
        if not saw_full:
            return "NO_FULL_SIZE_PAIR"
        if not saw_edge:
            return "EDGE"
        return "COVERAGE"

    def begin_update(self, engine, market_id: str, surge, batch_suite) -> None:
        if not self.enabled:
            return
        self.counts["MARKET_UPDATES"] += 1
        pair = engine.pairs.get(market_id)
        if pair is None:
            return
        if market_phase(pair) != MarketPhase.LIVE:
            self.counts["NOT_LIVE"] += 1
            return
        self.counts["LIVE_MARKET_UPDATES"] += 1
        asset = asset_from_slug(pair.slug) or "UNKNOWN"
        a = engine.books.get(pair.token_a)
        b = engine.books.get(pair.token_b)
        if a is not None and b is not None and a.ready and b.ready:
            self.counts["TWO_READY_BOOKS"] += 1
            now = time.monotonic()
            max_age = int(getattr(self.settings, "v187_max_book_age_ms", 25))
            if (
                a.updated_monotonic > 0
                and b.updated_monotonic > 0
                and (now - a.updated_monotonic) * 1000 <= max_age
                and (now - b.updated_monotonic) * 1000 <= max_age
            ):
                self.counts["TWO_FRESH_BOOKS"] += 1
        if surge is not None and surge.active:
            self.counts["SURGE_ACTIVE"] += 1

        raw_size = Decimal(str(getattr(self.settings, "v187_raw_size", Decimal("1"))))
        raw_metrics = self._pair_metrics(engine, pair, raw_size, max_age_ms=None)
        raw_edge = None
        if raw_metrics and not raw_metrics.get("no_full_pair") and not raw_metrics.get("stale"):
            self.counts["FULL_RAW_SIZE_PAIR"] += 1
            raw_edge = raw_metrics["edge"]
            if raw_metrics["coverage"] >= Decimal("1"):
                self.counts["COVERAGE_GE_1X"] += 1
            if raw_metrics["coverage"] >= Decimal("1.5"):
                self.counts["COVERAGE_GE_1_5X"] += 1
            for label, threshold in self.EDGE_THRESHOLDS:
                if raw_edge >= threshold:
                    self.counts[label] += 1

        raw = batch_suite.raw
        raw_before = {
            "placements": raw.placements if raw is not None else 0,
            "both": raw.both_filled if raw is not None else 0,
            "miss": raw.one_leg_miss if raw is not None else 0,
            "none": raw.neither_filled if raw is not None else 0,
            "equity": raw.equity.equity if raw is not None else ZERO,
        }
        bfok_before: dict[str, int] = {}
        bfok_reason: dict[str, str] = {}
        for variant in batch_suite.base.variants:
            bfok_before[variant.strategy] = variant.placements
            reason = self._classify_bfok(engine, market_id, surge, variant)
            bfok_reason[variant.strategy] = reason
            self.model_reasons[variant.strategy][reason] += 1

        self._active[market_id] = FunnelContextV187(
            market_id=market_id,
            slug=pair.slug,
            asset=asset,
            observed_at=_utc_now(),
            raw_before=raw_before,
            bfok_before=bfok_before,
            bfok_reason=bfok_reason,
            raw_edge=raw_edge,
        )

    def after_batch(self, market_id: str, batch_suite) -> None:
        if not self.enabled:
            return
        ctx = self._active.get(market_id)
        if ctx is None:
            return
        raw = batch_suite.raw
        if raw is not None:
            before = ctx.raw_before
            placement_delta = raw.placements - before["placements"]
            both_delta = raw.both_filled - before["both"]
            miss_delta = raw.one_leg_miss - before["miss"]
            none_delta = raw.neither_filled - before["none"]
            pnl_delta = raw.equity.equity - before["equity"]
            ctx.raw_attempted = placement_delta > 0
            ctx.raw_pnl = pnl_delta
            if ctx.raw_attempted:
                self.counts["RAW_ATTEMPTS"] += placement_delta
            if both_delta > 0:
                self.counts["RAW_BOTH_FILLED"] += both_delta
                ctx.raw_status = "BOTH_FILLED"
            elif miss_delta > 0:
                self.counts["RAW_ONE_LEG_MISS"] += miss_delta
                ctx.raw_status = "ONE_LEG_MISS"
            elif none_delta > 0:
                self.counts["RAW_NEITHER_FILLED"] += none_delta
                ctx.raw_status = "NEITHER_FILLED"
            if pnl_delta > ZERO:
                self.counts["RAW_WINS"] += 1
                ctx.raw_win = True
            elif pnl_delta < ZERO:
                self.counts["RAW_LOSSES"] += 1
            elif ctx.raw_attempted:
                self.counts["RAW_FLATS"] += 1

        for variant in batch_suite.base.variants:
            before = ctx.bfok_before.get(variant.strategy, variant.placements)
            entered = variant.placements > before
            reason = ctx.bfok_reason.get(variant.strategy, "UNKNOWN")
            state = "SUBMITTED" if entered else "BLOCKED"
            if entered:
                self.model_entries[variant.strategy] += 1
            elif reason == "QUALIFIED":
                reason = "QUALIFIED_BUT_NOT_SUBMITTED"
            ctx.protected[variant.strategy] = {"state": state, "reason": reason}
            if ctx.raw_win:
                if entered:
                    self.raw_win_model_entries[variant.strategy] += 1
                else:
                    self.raw_win_model_reasons[variant.strategy][reason] += 1

    def before_pfok(self, engine, market_id: str, surge, control_suite) -> None:
        if not self.enabled:
            return
        ctx = self._active.get(market_id)
        if ctx is None:
            return
        for variant in control_suite.variants:
            ctx.pfok_before[variant.strategy] = variant.candidates
            reason = self._classify_pfok(engine, market_id, surge, variant)
            ctx.pfok_reason[variant.strategy] = reason
            self.model_reasons[variant.strategy][reason] += 1

    def after_pfok(self, market_id: str, control_suite) -> None:
        if not self.enabled:
            return
        ctx = self._active.pop(market_id, None)
        if ctx is None:
            return
        for variant in control_suite.variants:
            before = ctx.pfok_before.get(variant.strategy, variant.candidates)
            entered = variant.candidates > before
            reason = ctx.pfok_reason.get(variant.strategy, "UNKNOWN")
            state = "CANDIDATE" if entered else "BLOCKED"
            if entered:
                self.model_entries[variant.strategy] += 1
            elif reason == "QUALIFIED":
                reason = "QUALIFIED_BUT_NOT_CANDIDATE"
            ctx.protected[variant.strategy] = {"state": state, "reason": reason}
            if ctx.raw_win:
                if entered:
                    self.raw_win_model_entries[variant.strategy] += 1
                else:
                    self.raw_win_model_reasons[variant.strategy][reason] += 1

        if ctx.raw_win:
            protected_states = list(ctx.protected.values())
            all_blocked = bool(protected_states) and all(v.get("state") == "BLOCKED" for v in protected_states)
            if all_blocked:
                self.counts["RAW_WINS_BLOCKED_BY_ALL_PROTECTED"] += 1
            self.recorder.write(
                "raw_win_attribution_v187",
                {
                    "market_id": ctx.market_id,
                    "slug": ctx.slug,
                    "asset": ctx.asset,
                    "observed_at": ctx.observed_at,
                    "raw_status": ctx.raw_status,
                    "raw_pnl": ctx.raw_pnl,
                    "raw_edge_per_share": ctx.raw_edge,
                    "all_protected_blocked": all_blocked,
                    "protected_models": ctx.protected,
                    "note": "Same-update entry attribution only; it does not guarantee a protected model would later have filled.",
                },
            )
        self._maybe_rollup()

    def _maybe_rollup(self) -> None:
        now = time.monotonic()
        if now - self._last_rollup < self.rollup_seconds:
            return
        self.recorder.write(
            "opportunity_funnel_rollup_v187",
            {
                "rolled_at": _utc_now(),
                "window_seconds": self.rollup_seconds,
                "counts": dict(self.counts),
                "model_entries": dict(self.model_entries),
                "model_reasons": {k: dict(v) for k, v in self.model_reasons.items()},
                "raw_win_model_entries": dict(self.raw_win_model_entries),
                "raw_win_model_reasons": {k: dict(v) for k, v in self.raw_win_model_reasons.items()},
            },
        )
        self.counts.clear()
        self.model_entries.clear()
        self.model_reasons.clear()
        self.raw_win_model_entries.clear()
        self.raw_win_model_reasons.clear()
        self._last_rollup = now
