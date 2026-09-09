from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from .discovery import MarketPhase, asset_from_slug, market_phase
from .fees import taker_fee
from .profit_fok_v185 import ProfitFOKEngineV185
from .research_context_v183 import PHASE183_RUN_ID
from .research_context_v184 import PHASE184_RUN_ID
from .research_context_v185 import PHASE185_RUN_ID
from .storage import JsonlRecorder


ZERO = Decimal("0")


class DiagnosedProfitFOKEngineV185(ProfitFOKEngineV185):
    """PFOK with low-volume gate telemetry for zero-candidate diagnosis."""

    def __init__(self, settings, recorder: JsonlRecorder) -> None:
        super().__init__(settings, recorder)
        self._gate_sample_after: dict[str, float] = {}

    @staticmethod
    def _depth_at_limit_raw(levels, limit: Decimal) -> Decimal:
        return sum((qty for px, qty in levels.items() if px <= limit), ZERO)

    def _gate_snapshot(self, engine, pair, now: float) -> dict[str, Any]:
        a = engine.books.get(pair.token_a)
        b = engine.books.get(pair.token_b)
        age_a = None
        age_b = None
        if a is not None and a.updated_monotonic > 0:
            age_a = Decimal(str(max(0.0, (now - a.updated_monotonic) * 1000)))
        if b is not None and b.updated_monotonic > 0:
            age_b = Decimal(str(max(0.0, (now - b.updated_monotonic) * 1000)))

        payload: dict[str, Any] = {
            "phase183_run_id": PHASE183_RUN_ID,
            "phase184_run_id": PHASE184_RUN_ID,
            "phase185_run_id": PHASE185_RUN_ID,
            "strategy": self.strategy,
            "mode": "PROFIT_FOK_V185",
            "direction": self.direction,
            "market_id": pair.market_id,
            "slug": pair.slug,
            "asset": asset_from_slug(pair.slug) or "UNKNOWN",
            "book_age_a_ms": age_a,
            "book_age_b_ms": age_b,
        }

        if a is None or b is None or not a.ready or not b.ready:
            payload["gate_reason"] = "BOOK_NOT_READY"
            return payload

        max_age = Decimal(self.settings.v185_max_book_age_ms)
        if age_a is None or age_b is None or age_a > max_age or age_b > max_age:
            payload["gate_reason"] = "BOOK_TOO_OLD"
            return payload

        best: dict[str, Any] | None = None
        for shares in sorted(self.settings.v185_profit_sizes, reverse=True):
            qa = a.quote_buy(shares)
            qb = b.quote_buy(shares)
            if qa is None or qb is None:
                continue
            fee_a = taker_fee(qa.segments, self.settings.crypto_taker_fee_rate)
            fee_b = taker_fee(qb.segments, self.settings.crypto_taker_fee_rate)
            pnl = shares - qa.notional - qb.notional - fee_a - fee_b
            edge = pnl / shares
            cov_a = self._depth_at_limit_raw(a.asks, qa.marginal_price) / shares
            cov_b = self._depth_at_limit_raw(b.asks, qb.marginal_price) / shares
            item = {
                "shares": shares,
                "edge": edge,
                "coverage_a": cov_a,
                "coverage_b": cov_b,
                "coverage": min(cov_a, cov_b),
            }
            if best is None or edge > best["edge"]:
                best = item

        if best is None:
            payload["gate_reason"] = "NO_FULL_SIZE_PAIR"
            return payload

        payload.update(
            {
                "sample_shares": best["shares"],
                "sample_edge_per_share": best["edge"],
                "sample_coverage_a": best["coverage_a"],
                "sample_coverage_b": best["coverage_b"],
                "sample_coverage": best["coverage"],
            }
        )
        if best["edge"] < self.settings.v185_detection_min_edge_per_share:
            payload["gate_reason"] = "EDGE_BELOW_DETECTION"
        elif best["coverage"] < self.settings.v185_detection_coverage_multiple:
            payload["gate_reason"] = "DEPTH_BELOW_DETECTION"
        else:
            payload["gate_reason"] = "QUALIFIED"
        return payload

    def on_market_update(self, engine, market_id: str, surge=None) -> None:
        now = time.monotonic()
        pair = engine.pairs.get(market_id)
        if (
            self.settings.v185_profit_fok_enabled
            and pair is not None
            and market_phase(pair) == MarketPhase.LIVE
            and market_id not in self.pending
            and now >= self.cooldown_until.get(market_id, 0.0)
        ):
            due = self._gate_sample_after.get(market_id, 0.0)
            if now >= due:
                payload = self._gate_snapshot(engine, pair, now)
                if self.settings.v185_use_surge_gate and surge is not None and surge.active:
                    payload["gate_reason"] = "SURGE_BLOCK"
                self.recorder.write("profit_fok_gate_sample_v185", payload)
                self._gate_sample_after[market_id] = now + self.settings.v185_gate_sample_interval_ms / 1000

        super().on_market_update(engine, market_id, surge)


class DiagnosedProfitFOKSuiteV185:
    def __init__(self, settings, recorder: JsonlRecorder) -> None:
        self.settings = settings
        self.engine = DiagnosedProfitFOKEngineV185(settings, recorder)
        self.variants = [self.engine]

    def on_market_update(self, engine, market_id: str, surge=None) -> None:
        self.engine.on_market_update(engine, market_id, surge)

    def process_due(self, engine) -> None:
        self.engine.process_due(engine)

    def diagnostic_rows(self) -> list[dict[str, Any]]:
        return [self.engine.diagnostic_row()]

    def ranked_rows(self) -> list[dict[str, Any]]:
        row = self.engine.diagnostic_row()
        return [row] if row["placements"] > 0 else []
