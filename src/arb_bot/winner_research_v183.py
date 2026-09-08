from __future__ import annotations

from decimal import Decimal

from .selective_research_v183 import (
    SelectiveHybridVariantV183,
    SelectiveMakerVariantV183,
    SelectivePairedMakerVariantV183,
)
from .winner_research_v182 import WinnerResearchSuiteV182


class WinnerResearchSuiteV183(WinnerResearchSuiteV182):
    """Phase 1.8.2 suite with additive Phase 1.8.3 counterfactual telemetry."""

    def __init__(self, settings, recorder) -> None:
        super().__init__(settings, recorder)

        self.selective_hybrids = []
        self.selective_paired_makers = []
        self.selective_makers = []
        if settings.v181_selective_enabled:
            self.selective_hybrids = [
                SelectiveHybridVariantV183(
                    settings,
                    recorder,
                    self.regime,
                    min_imbalance=Decimal("2"),
                ),
                SelectiveHybridVariantV183(
                    settings,
                    recorder,
                    self.regime,
                    min_imbalance=Decimal("3"),
                ),
                SelectiveHybridVariantV183(
                    settings,
                    recorder,
                    self.regime,
                    min_imbalance=Decimal("4"),
                ),
                SelectiveHybridVariantV183(
                    settings,
                    recorder,
                    self.regime,
                    min_imbalance=Decimal("2"),
                    small_queue_cap=settings.v181_hybrid_small_queue_cap,
                ),
            ]
            self.selective_paired_makers = [
                SelectivePairedMakerVariantV183(
                    settings,
                    recorder,
                    self.regime,
                    max_pair=Decimal("0.95"),
                    max_queue=Decimal("10"),
                ),
                SelectivePairedMakerVariantV183(
                    settings,
                    recorder,
                    self.regime,
                    max_pair=Decimal("0.97"),
                    max_queue=Decimal("10"),
                ),
                SelectivePairedMakerVariantV183(
                    settings,
                    recorder,
                    self.regime,
                    max_pair=Decimal("0.97"),
                    max_queue=Decimal("15"),
                ),
                SelectivePairedMakerVariantV183(
                    settings,
                    recorder,
                    self.regime,
                    max_pair=Decimal("0.97"),
                    max_queue=Decimal("25"),
                ),
            ]
            self.selective_makers = [
                SelectiveMakerVariantV183(
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
