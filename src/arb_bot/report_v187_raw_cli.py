from __future__ import annotations

from . import report_v187_cli as _base


def cli() -> None:
    # Reuse the Phase 1.8.7 report machinery but include the deliberately
    # ungated BFOK-RAW diagnostic in BFOK, latency, economics and episode tables.
    if "BFOK-RAW" not in _base.BFOK_NAMES:
        _base.BFOK_NAMES.append("BFOK-RAW")
    if "BFOK-RAW" not in _base.CORE_NAMES:
        _base.CORE_NAMES.append("BFOK-RAW")
    _base.cli()


if __name__ == "__main__":
    cli()
