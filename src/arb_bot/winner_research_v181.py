from __future__ import annotations

from decimal import Decimal

from .discovery import asset_from_slug
from .maker_research import MarketRegimeTracker, ZERO
from .runtime_controls_v18 import DEFAULT_PROFILE, runtime_controls
from .selective_research_v181 import (
    InstrumentedMultiAssetQueueAwareVariantEngine,
    InstrumentedPairedMakerVariantEngine,
    SelectiveHybridVariant,
    SelectiveMakerVariant,
    SelectivePairedMakerVariant,
)
from .storage import JsonlRecorder
from .strategy import ArbitrageEngine


class WinnerResearchSuiteV181:
    """Runtime-controlled maker research plus selective Phase 1.8.1 variants."""

    def __init__(self, settings, recorder: JsonlRecorder) -> None:
        self.settings = settings
        self.recorder = recorder
        self.regime = MarketRegimeTracker(settings)

        targets = (
            Decimal("0.99"),
            Decimal("0.98"),
            Decimal("0.97"),
            Decimal("0.96"),
        )
        queues = (
            Decimal("25"),
            Decimal("50"),
            Decimal("100"),
            Decimal("250"),
        )

        self.makers = [
            InstrumentedMultiAssetQueueAwareVariantEngine(
                settings,
                recorder,
                self.regime,
                mode="MAKER",
                target_pair=target,
            )
            for target in targets
        ]
        self.hybrids = [
            InstrumentedMultiAssetQueueAwareVariantEngine(
                settings,
                recorder,
                self.regime,
                mode="HYBRID",
                target_pair=target,
            )
            for target in targets
        ]
        self.paired_makers = [
            InstrumentedPairedMakerVariantEngine(
                settings,
                recorder,
                self.regime,
                max_queue=max_queue,
            )
            for max_queue in queues
        ]

        self.selective_hybrids = []
        self.selective_paired_makers = []
        self.selective_makers = []
        if settings.v181_selective_enabled:
            self.selective_hybrids = [
                SelectiveHybridVariant(
                    settings,
                    recorder,
                    self.regime,
                    min_imbalance=Decimal("2"),
                ),
                SelectiveHybridVariant(
                    settings,
                    recorder,
                    self.regime,
                    min_imbalance=Decimal("3"),
                ),
                SelectiveHybridVariant(
                    settings,
                    recorder,
                    self.regime,
                    min_imbalance=Decimal("4"),
                ),
                SelectiveHybridVariant(
                    settings,
                    recorder,
                    self.regime,
                    min_imbalance=Decimal("2"),
                    small_queue_cap=settings.v181_hybrid_small_queue_cap,
                ),
            ]
            self.selective_paired_makers = [
                SelectivePairedMakerVariant(
                    settings,
                    recorder,
                    self.regime,
                    max_pair=Decimal("0.95"),
                    max_queue=Decimal("10"),
                ),
                SelectivePairedMakerVariant(
                    settings,
                    recorder,
                    self.regime,
                    max_pair=Decimal("0.97"),
                    max_queue=Decimal("10"),
                ),
                SelectivePairedMakerVariant(
                    settings,
                    recorder,
                    self.regime,
                    max_pair=Decimal("0.97"),
                    max_queue=Decimal("15"),
                ),
                SelectivePairedMakerVariant(
                    settings,
                    recorder,
                    self.regime,
                    max_pair=Decimal("0.97"),
                    max_queue=Decimal("25"),
                ),
            ]
            self.selective_makers = [
                SelectiveMakerVariant(
                    settings,
                    recorder,
                    self.regime,
                    target_pair=Decimal("0.97"),
                    max_queue=Decimal("10"),
                )
            ]

        self.selective_variants = [
            *self.selective_hybrids,
            *self.selective_paired_makers,
            *self.selective_makers,
        ]
        self.variants = [
            *self.makers,
            *self.hybrids,
            *self.paired_makers,
            *self.selective_variants,
        ]

        # Selective variants default ON across the observed feed. Their own
        # microstructure gates are deliberately strict; model×asset switches
        # remain available in ARB//TERM for out-of-sample attribution.
        all_assets = set(settings.market_assets)
        for variant in self.selective_variants:
            DEFAULT_PROFILE.setdefault(variant.strategy_name, set(all_assets))

        runtime_controls.configure(
            (variant.strategy_name for variant in self.variants),
            settings.market_assets,
        )
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
        # Always service open campaigns after a UI toggle is disabled.
        for variant in self.variants:
            variant.process_due(engine)

    def diagnostic_rows(self) -> list[dict]:
        rows = [variant.diagnostic_row() for variant in self.variants]
        control = {
            row["model"]: row
            for row in runtime_controls.snapshot()["models"]
        }
        for row in rows:
            info = control.get(row["strategy"], {})
            row["runtime_enabled"] = bool(info.get("enabled"))
            row["enabled_assets"] = info.get("enabled_assets", [])
        return rows

    @property
    def maker_total_pnl(self) -> Decimal:
        return sum(
            (
                variant.total_pnl
                for variant in [*self.makers, *self.selective_makers]
            ),
            ZERO,
        )

    @property
    def hybrid_total_pnl(self) -> Decimal:
        return sum(
            (
                variant.total_pnl
                for variant in [*self.hybrids, *self.selective_hybrids]
            ),
            ZERO,
        )

    @property
    def paired_maker_total_pnl(self) -> Decimal:
        return sum(
            (
                variant.total_pnl
                for variant in [
                    *self.paired_makers,
                    *self.selective_paired_makers,
                ]
            ),
            ZERO,
        )
