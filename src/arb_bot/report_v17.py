from __future__ import annotations

from . import report as _base


# Reuse the mature Phase 1.6 report parser while teaching it that ideal atomic
# captures are research summaries. Their strategy names and mode explicitly
# identify them as non-executable benchmark P&L.
_base.RESEARCH_SUMMARY_EVENT_TYPES.add("atomic_benchmark_capture")

build_report = _base.build_report
write_csv = _base.write_csv


def cli() -> None:
    _base.cli()
