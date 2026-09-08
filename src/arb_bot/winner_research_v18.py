from __future__ import annotations

from decimal import Decimal

from .discovery import asset_from_slug
from .maker_research import MarketRegimeTracker, ZERO
from .maker_research_v17 import MultiAssetQueueAwareVariantEngine, PairedMakerVariantEngine
from .storage import JsonlRecorder
from .strategy import ArbitrageEngine


class WinnerResearchSuiteV18:
    """Evidence-gated Phase 1.8 maker research.

    Active executable-shadow models by default:
      - HYBRID-99
      - HYBRID-98
      - PMAKER-Q100
      - PMAKER-Q250

    Only configured ``winner_assets`` are allowed to start new campaigns.
    Historical models remain in the codebase and can be re-enabled via settings,
    but are not instantiated here as active Phase 1.8 experiments.
    """

    def __init__(self, settings, recorder: JsonlRecorder) -> None:
        self.settings = settings
        self.recorder = recorder
        self.regime = MarketRegimeTracker(settings)

        self.makers: list[MultiAssetQueueAwareVariantEngine] = []
        self.hybrids = (
            [
                MultiAssetQueueAwareVariantEngine(
                    settings,
                    recorder,
                    self.regime,
                    mode="HYBRID",
                    target_pair=target,
                )
                for target in settings.maker_variant_targets
                if target in {Decimal("0.99"), Decimal("0.98")}
            ]
            if settings.hybrid_enabled
            else []
        )
        self.paired_makers = (
            [
                PairedMakerVariantEngine(
                    settings,
                    recorder,
                    self.regime,
                    max_queue=max_queue,
                )
                for max_queue in settings.paired_maker_max_queues
                if max_queue in {Decimal("100"), Decimal("250")}
            ]
            if settings.paired_maker_enabled
            else []
        )
        self.variants = [*self.hybrids, *self.paired_makers]
        self.asset_skips = 0

    def on_market_update(self, engine: ArbitrageEngine, market_id: str) -> None:
        pair = engine.pairs.get(market_id)
        if pair is None:
            return
        asset = asset_from_slug(pair.slug)
        if asset not in self.settings.winner_assets:
            self.asset_skips += 1
            return

        surge = self.regime.observe(engine, market_id)
        for variant in self.variants:
            variant.on_market_update(engine, market_id, surge)

    def process_due(self, engine: ArbitrageEngine) -> None:
        for variant in self.variants:
            variant.process_due(engine)

    def diagnostic_rows(self) -> list[dict]:
        rows = [variant.diagnostic_row() for variant in self.variants]
        for row in rows:
            row["winner_profile"] = True
            row["winner_assets"] = self.settings.winner_assets
        return rows

    @property
    def maker_total_pnl(self) -> Decimal:
        return ZERO

    @property
    def hybrid_total_pnl(self) -> Decimal:
        return sum((variant.total_pnl for variant in self.hybrids), ZERO)

    @property
    def paired_maker_total_pnl(self) -> Decimal:
        return sum((variant.total_pnl for variant in self.paired_makers), ZERO)
