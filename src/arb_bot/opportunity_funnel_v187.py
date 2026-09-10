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
    ready_books: bool
    fresh_books: bool
    raw_before: dict[str, Any]
    bfok_before: dict[str, int]
    bfok_precondition: dict[str, str | None]
    pfok_before: dict[str, int] = field(default_factory=dict)
    pfok_reason: dict[str, str] = field(default_factory=dict)
    standard_metrics: dict[Decimal, dict[str, Any]] = field(default_factory=dict)
    extra_metrics: dict[Decimal, dict[str, Any] | None] = field(default_factory=dict)
    raw_attempted: bool = False
    raw_status: str | None = None
    raw_pnl: Decimal = ZERO
    raw_edge: Decimal | None = None
    raw_win: bool = False
    protected: dict[str, dict[str, Any]] = field(default_factory=dict)


class OpportunityFunnelV187:
    """Low-overhead funnel and RAW-win/protected-entry attribution.

    The funnel reuses RAW and protected BFOK snapshots already built for the
    strategies. It does not rebuild the full order book for every model. Only
    PFOK's unique 2-share size can require one additional quote, memoized for
    the current market update.
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

    def _quote_metrics(self, engine, pair, shares: Decimal) -> dict[str, Any] | None:
        a = engine.books.get(pair.token_a)
        b = engine.books.get(pair.token_b)
        if a is None or b is None or not a.ready or not b.ready:
            return None
        qa = a.quote_buy(shares)
        qb = b.quote_buy(shares)
        if qa is None or qb is None:
            return None
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

    @staticmethod
    def _precondition(market_id: str, surge, variant, *, family: str) -> str | None:
        now = time.monotonic()
        if market_id in variant.pending:
            return "PENDING"
        if now < variant.cooldown_until.get(market_id, 0.0):
            return "COOLDOWN"
        use_surge = (
            variant.settings.v187_use_surge_gate
            if family == "BFOK"
            else variant.settings.v185_use_surge_gate
        )
        if use_surge and surge is not None and surge.active:
            return "SURGE"
        return None

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
        ready = bool(a is not None and b is not None and a.ready and b.ready)
        fresh = False
        if ready:
            self.counts["TWO_READY_BOOKS"] += 1
            now = time.monotonic()
            max_age = int(getattr(self.settings, "v187_max_book_age_ms", 25))
            fresh = bool(
                a.updated_monotonic > 0
                and b.updated_monotonic > 0
                and (now - a.updated_monotonic) * 1000 <= max_age
                and (now - b.updated_monotonic) * 1000 <= max_age
            )
            if fresh:
                self.counts["TWO_FRESH_BOOKS"] += 1
        if surge is not None and surge.active:
            self.counts["SURGE_ACTIVE"] += 1

        raw = batch_suite.raw
        raw_before = {
            "placements": raw.placements if raw is not None else 0,
            "both": raw.both_filled if raw is not None else 0,
            "miss": raw.one_leg_miss if raw is not None else 0,
            "none": raw.neither_filled if raw is not None else 0,
            "equity": raw.equity.equity if raw is not None else ZERO,
        }
        bfok_before: dict[str, int] = {}
        bfok_precondition: dict[str, str | None] = {}
        for variant in batch_suite.base.variants:
            bfok_before[variant.strategy] = variant.placements
            bfok_precondition[variant.strategy] = self._precondition(
                market_id, surge, variant, family="BFOK"
            )

        self._active[market_id] = FunnelContextV187(
            market_id=market_id,
            slug=pair.slug,
            asset=asset,
            observed_at=_utc_now(),
            ready_books=ready,
            fresh_books=fresh,
            raw_before=raw_before,
            bfok_before=bfok_before,
            bfok_precondition=bfok_precondition,
        )

    def _bfok_reason(self, ctx: FunnelContextV187, variant) -> str:
        pre = ctx.bfok_precondition.get(variant.strategy)
        if pre:
            return pre
        if not ctx.ready_books:
            return "BOOK_NOT_READY"
        if not ctx.fresh_books:
            return "STALE_BOOK"

        if variant.fixed_size is not None:
            metrics = ctx.standard_metrics.get(variant.fixed_size)
            if metrics is None:
                return "NO_FULL_SIZE_PAIR"
            if metrics["edge"] < variant.settings.v187_detection_min_edge_per_share:
                return "EDGE"
            if metrics["coverage"] < variant.settings.v187_detection_coverage_multiple:
                return "COVERAGE"
            return "QUALIFIED"

        saw_full = False
        saw_edge = False
        saw_cov = False
        accepted = False
        for shares in variant.settings.v187_ev_sizes:
            metrics = ctx.standard_metrics.get(shares)
            if metrics is None:
                continue
            saw_full = True
            if metrics["edge"] < variant.settings.v187_detection_min_edge_per_share:
                continue
            saw_edge = True
            if metrics["coverage"] < variant.settings.v187_detection_coverage_multiple:
                continue
            saw_cov = True
            estimate = variant.risk_book.estimate(
                asset=ctx.asset,
                shares=shares,
                detected_edge=metrics["edge"],
                detected_pnl=metrics["pnl"],
            )
            if estimate["expected_pnl"] >= variant.settings.v187_ev_min_expected_pnl:
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

    def after_batch(self, market_id: str, batch_suite) -> None:
        if not self.enabled:
            return
        ctx = self._active.get(market_id)
        if ctx is None:
            return

        standard = batch_suite.last_standard_snapshot
        if standard is not None:
            ctx.standard_metrics = dict(standard.metrics)

        raw_size = Decimal(str(getattr(self.settings, "v187_raw_size", Decimal("1"))))
        raw_snapshot = batch_suite.last_raw_snapshot
        raw_metrics = raw_snapshot.metrics.get(raw_size) if raw_snapshot is not None else None
        if raw_metrics is not None:
            self.counts["FULL_RAW_SIZE_PAIR"] += 1
            ctx.raw_edge = raw_metrics["edge"]
            if raw_metrics["coverage"] >= Decimal("1"):
                self.counts["COVERAGE_GE_1X"] += 1
            if raw_metrics["coverage"] >= Decimal("1.5"):
                self.counts["COVERAGE_GE_1_5X"] += 1
            for label, threshold in self.EDGE_THRESHOLDS:
                if ctx.raw_edge >= threshold:
                    self.counts[label] += 1

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
            reason = self._bfok_reason(ctx, variant)
            self.model_reasons[variant.strategy][reason] += 1
            before = ctx.bfok_before.get(variant.strategy, variant.placements)
            entered = variant.placements > before
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

    def _pfok_metrics(self, engine, ctx: FunnelContextV187, pair, shares: Decimal) -> dict[str, Any] | None:
        if shares in ctx.standard_metrics:
            return ctx.standard_metrics[shares]
        if shares not in ctx.extra_metrics:
            ctx.extra_metrics[shares] = self._quote_metrics(engine, pair, shares)
        return ctx.extra_metrics[shares]

    def _classify_pfok(self, engine, ctx: FunnelContextV187, surge, variant) -> str:
        pre = self._precondition(ctx.market_id, surge, variant, family="PFOK")
        if pre:
            return pre
        if not ctx.ready_books:
            return "BOOK_NOT_READY"
        if not ctx.fresh_books:
            return "STALE_BOOK"
        pair = engine.pairs.get(ctx.market_id)
        if pair is None:
            return "NOT_LIVE"
        saw_full = False
        saw_edge = False
        for shares in sorted(variant.settings.v185_profit_sizes, reverse=True):
            metrics = self._pfok_metrics(engine, ctx, pair, shares)
            if metrics is None:
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

    def before_pfok(self, engine, market_id: str, surge, control_suite) -> None:
        if not self.enabled:
            return
        ctx = self._active.get(market_id)
        if ctx is None:
            return
        for variant in control_suite.variants:
            ctx.pfok_before[variant.strategy] = variant.candidates
            reason = self._classify_pfok(engine, ctx, surge, variant)
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
                    "note": "Same-update entry attribution only; protected models still face their later modeled execution timing.",
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
