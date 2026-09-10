from __future__ import annotations

from . import report_v187_cli as _base


def cli() -> None:
    # Reuse the Phase 1.8.7 report machinery but include the deliberately
    # ungated BFOK-RAW diagnostic in BFOK, latency, economics and episode tables.
    if "BFOK-RAW" not in _base.BFOK_NAMES:
        _base.BFOK_NAMES.append("BFOK-RAW")
    if "BFOK-RAW" not in _base.CORE_NAMES:
        _base.CORE_NAMES.append("BFOK-RAW")

    original_bfok_table = _base._bfok_table

    def bfok_table_with_raw_note(path, asset_filter):
        text = original_bfok_table(path, asset_filter)
        return (
            text
            + "\nBFOK-RAW: UNGATED ZERO-LATENCY UPPER-BOUND DIAGNOSTIC. "
              "It trades every structurally executable 1-share pair it observes, "
              "including negative-edge states. Do not treat RAW P&L as deployable live P&L."
        )

    _base._bfok_table = bfok_table_with_raw_note
    try:
        _base.cli()
    finally:
        _base._bfok_table = original_bfok_table


if __name__ == "__main__":
    cli()
