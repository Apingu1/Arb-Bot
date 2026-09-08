from __future__ import annotations

from decimal import Decimal
from statistics import median
from typing import Any

from .dual_fok_research import (
    DualFOKResearchSuite,
    DualFOKVariantEngine,
    DualFOKVariantSpec,
    ZERO,
    _avg,
    _utc_now,
)


class DualFOKVariantEngineV17(DualFOKVariantEngine):
    """Phase 1.7 wrapper that preserves asset/slug attribution in lifetime events."""

    def __init__(self, settings, recorder, spec: DualFOKVariantSpec) -> None:
        super().__init__(settings, recorder, spec)
        self._market_slugs: dict[str, str] = {}

    def on_market_update(self, engine, market_id: str, surge=None) -> None:
        pair = engine.pairs.get(market_id)
        if pair is not None:
            self._market_slugs[market_id] = pair.slug
        super().on_market_update(engine, market_id, surge)

    def _close_qualification(self, market_id: str, now: float, reason: str) -> None:
        state = self.qualifying.pop(market_id, None)
        if state is None:
            return
        lifetime = Decimal(str(max(0.0, (now - state.started_at) * 1000)))
        self.opportunities_ended += 1
        self.opportunity_lifetimes_ms.append(lifetime)
        self.recorder.write(
            "dual_fok_opportunity_lifetime",
            {
                "strategy": self.spec.strategy,
                "direction": self.spec.direction,
                "market_id": market_id,
                "slug": self._market_slugs.get(market_id),
                "started_at": state.started_at_utc,
                "ended_at": _utc_now(),
                "lifetime_ms": lifetime,
                "peak_edge_per_share": state.peak_edge,
                "min_coverage_multiple": state.min_coverage,
                "attempt_armed": state.armed,
                "end_reason": reason,
            },
        )


class DualFOKResearchSuiteV17(DualFOKResearchSuite):
    """Same Phase 1.6 mechanics with clean labels and asset-aware lifetimes."""

    @staticmethod
    def _size_code(value: Decimal) -> str:
        return format(value.normalize(), "f")

    def _make(
        self,
        *,
        prefix: str,
        direction: str,
        shares: Decimal,
        edge: Decimal,
        skew: int,
        coverage: Decimal,
        stability: int,
        family: str,
    ) -> DualFOKVariantEngine:
        strategy = (
            f"{prefix}-{family}-S{self._size_code(shares)}-E{self._edge_code(edge)}"
            f"-SK{skew}-C{self._cov_code(coverage)}-ST{stability}"
        )
        return DualFOKVariantEngineV17(
            self.settings,
            self.recorder,
            DualFOKVariantSpec(
                strategy=strategy,
                direction=direction,
                shares=shares,
                edge_target=edge,
                arrival_skew_ms=skew,
                coverage_multiple=coverage,
                stability_ms=stability,
            ),
        )
