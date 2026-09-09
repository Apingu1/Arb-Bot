from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal

from .config import _bool, _decimal_tuple, _int
from .config_v185 import SettingsV185


@dataclass(frozen=True, slots=True)
class SettingsV186(SettingsV185):
    """Phase 1.8.6 latency-first PFOK controls.

    All Phase 1.8.5 PFOK/frontier settings remain inherited and unchanged.
    Phase 1.8.6 adds a separate prioritized fast-path frontier plus lower-volume
    telemetry so we can measure the true execution frontier without weakening
    the profitable control strategy.
    """

    v186_fast_pfok_enabled: bool = field(
        default_factory=lambda: _bool("V186_FAST_PFOK_ENABLED", True)
    )
    v186_fast_snapshot_sizes: tuple[Decimal, ...] = field(
        default_factory=lambda: _decimal_tuple("V186_FAST_SNAPSHOT_SIZES", "1,2,5,10,20")
    )
    v186_fast1_latency_ms: int = field(
        default_factory=lambda: _int("V186_FAST1_LATENCY_MS", 1)
    )
    v186_fast2_latency_ms: int = field(
        default_factory=lambda: _int("V186_FAST2_LATENCY_MS", 2)
    )
    v186_fast_size_latency_ms: int = field(
        default_factory=lambda: _int("V186_FAST_SIZE_LATENCY_MS", 1)
    )

    # Telemetry optimization. Raw Phase 1.8.5 gate samples are aggregated in
    # memory and periodically emitted as compact rollups in 1.8.6.
    v186_gate_rollup_seconds: int = field(
        default_factory=lambda: _int("V186_GATE_ROLLUP_SECONDS", 5)
    )
    v186_recorder_flush_ms: int = field(
        default_factory=lambda: _int("V186_RECORDER_FLUSH_MS", 250)
    )
    v186_current_session_path: str = field(
        default_factory=lambda: os.getenv(
            "V186_CURRENT_SESSION_PATH", "data/current_session_v186.jsonl"
        )
    )

    # Keep a bounded number of values per rollup interval for percentile
    # summaries; counts themselves are exact.
    v186_rollup_sample_cap: int = field(
        default_factory=lambda: _int("V186_ROLLUP_SAMPLE_CAP", 512)
    )
