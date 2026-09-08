from __future__ import annotations

from decimal import Decimal

from .discovery import asset_from_slug
from .maker_research import MarketRegimeTracker, ZERO
from .maker_research_v17 import MultiAssetQueueAwareVariantEngine, PairedMakerVariantEngine
from .runtime_controls_v18 import runtime_controls
from .storage import JsonlRecorder
from .strategy import ArbitrageEngine


class WinnerResearchSuiteV18:
    """Phase 1.8 runtime-controlled maker-family research suite.

    All historical MAKER/HYBRID/PMAKER variants are instantiated so they can be
    switched on/off from ARB//TERM without restarting the bot. A model only
    receives a market update when that exact model×asset combination is enabled.
    Existing open campaigns continue through ``process_due`` so disabling a
    model never strands simulated inventory.
    """

    def __init__(self, settings, recorder: JsonlRecorder) -> None:
        self.settings = settings
        self.recorder = recorder
        self.regime = MarketRegimeTracker(settings)

        targets = (Decimal("0.99"), Decimal("0.98"), Decimal("0.97"), Decimal("0.96"))
        queues = (Decimal("25"), Decimal("50"), Decimal("100"), Decimal("250"))

        self.makers = [
            MultiAssetQueueAwareVariantEngine(settings, recorder, self.regime, mode="MAKER", target_pair=target)
            for target in targets
        ]
        self.hybrids = [
            MultiAssetQueueAwareVariantEngine(settings, recorder, self.regime, mode="HYBRID", target_pair=target)
            for target in targets
        ]
        self.paired_makers = [
            PairedMakerVariantEngine(settings, recorder, self.regime, max_queue=max_queue)
            for max_queue in queues
        ]
        self.variants = [*self.makers, *self.hybrids, *self.paired_makers]
        runtime_controls.configure((variant.strategy_name for variant in self.variants), settings.market_assets)
        self.asset_skips = 0

    def on_market_update(self, engine: ArbitrageEngine, market_id: str) -> None:
        pair = engine.pairs.get(market_id)
        if pair is None:
            return
        asset = asset_from_slug(pair.slug)
        if not asset:
            return

        surge = self.regime.observe(engine, market_id)
        active = False
        for variant in self.variants:
            if runtime_controls.enabled_for(variant.strategy_name, asset):
                active = True
                variant.on_market_update(engine, market_id, surge)
        if not active:
            self.asset_skips += 1

    def process_due(self, engine: ArbitrageEngine) -> None:
        # Always service existing campaigns even after a UI toggle is switched
        # off so simulated inventory is safely completed/cancelled/unwound.
        for variant in self.variants:
            variant.process_due(engine)

    def diagnostic_rows(self) -> list[dict]:
        rows = [variant.diagnostic_row() for variant in self.variants]
        control = {row["model"]: row for row in runtime_controls.snapshot()["models"]}
        for row in rows:
            info = control.get(row["strategy"], {})
            row["runtime_enabled"] = bool(info.get("enabled"))
            row["enabled_assets"] = info.get("enabled_assets", [])
        return rows

    @property
    def maker_total_pnl(self) -> Decimal:
        return sum((variant.total_pnl for variant in self.makers), ZERO)

    @property
    def hybrid_total_pnl(self) -> Decimal:
        return sum((variant.total_pnl for variant in self.hybrids), ZERO)

    @property
    def paired_maker_total_pnl(self) -> Decimal:
        return sum((variant.total_pnl for variant in self.paired_makers), ZERO)
