from __future__ import annotations

from decimal import Decimal

from .selective_research_v182 import (
    SelectiveHybridVariantV182,
    SelectiveMakerVariantV182,
    SelectivePairedMakerVariantV182,
)
from .winner_research_v181 import WinnerResearchSuiteV181


class WinnerResearchSuiteV182(WinnerResearchSuiteV181):
    """Phase 1.8.1 research suite with additive Phase 1.8.2 telemetry.

    Trading behaviour and model names are intentionally unchanged. Only the
    selective engines are replaced with telemetry-enhanced subclasses so the
    existing runtime model×asset controls continue to work without migration.
    """

    def __init__(self, settings, recorder) -> None:
        super().__init__(settings, recorder)

        self.selective_hybrids = []
        self.selective_paired_makers = []
        self.selective_makers = []
        if settings.v181_selective_enabled:
            self.selective_hybrids = [
                SelectiveHybridVariantV182(
                    settings,
                    recorder,
                    self.regime,
                    min_imbalance=Decimal("2"),
                ),
                SelectiveHybridVariantV182(
                    settings,
                    recorder,
                    self.regime,
                    min_imbalance=Decimal("3"),
                ),
                SelectiveHybridVariantV182(
                    settings,
                    recorder,
                    self.regime,
                    min_imbalance=Decimal("4"),
                ),
                SelectiveHybridVariantV182(
                    settings,
                    recorder,
                    self.regime,
                    min_imbalance=Decimal("2"),
                    small_queue_cap=settings.v181_hybrid_small_queue_cap,
                ),
            ]
            self.selective_paired_makers = [
                SelectivePairedMakerVariantV182(
                    settings,
                    recorder,
                    self.regime,
                    max_pair=Decimal("0.95"),
                    max_queue=Decimal("10"),
                ),
                SelectivePairedMakerVariantV182(
                    settings,
                    recorder,
                    self.regime,
                    max_pair=Decimal("0.97"),
                    max_queue=Decimal("10"),
                ),
                SelectivePairedMakerVariantV182(
                    settings,
                    recorder,
                    self.regime,
                    max_pair=Decimal("0.97"),
                    max_queue=Decimal("15"),
                ),
                SelectivePairedMakerVariantV182(
                    settings,
                    recorder,
                    self.regime,
                    max_pair=Decimal("0.97"),
                    max_queue=Decimal("25"),
                ),
            ]
            self.selective_makers = [
                SelectiveMakerVariantV182(
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
