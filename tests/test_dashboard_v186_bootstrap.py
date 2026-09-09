from __future__ import annotations

import json
from decimal import Decimal

from arb_bot.config_v186 import SettingsV186
from arb_bot.dashboard_v186 import DashboardStateV186


def _equity(pnl: str, *, recorded_at: str) -> dict:
    return {
        "strategy": "PFOK",
        "pnl_delta": pnl,
        "slug": "btc-updown-15m-1999999800",
        "status": "BOTH_FILLED",
        "action": "MERGE_COMPLETE_SET",
        "recorded_at": recorded_at,
    }


def test_v186_history_bootstrap_uses_frozen_prefix_and_keeps_session_separate(tmp_path):
    path = tmp_path / "shadow_events.jsonl"
    historical = _equity("1.0", recorded_at="2026-09-09T10:00:00Z")
    path.write_text(
        json.dumps({"event_type": "strategy_equity", "payload": historical}) + "\n",
        encoding="utf-8",
    )

    state = DashboardStateV186(SettingsV186())
    state.bootstrap(str(path))

    # This row is appended after bootstrap has frozen the historical byte
    # boundary. It represents a live event and must not be replayed by the
    # background historical worker as well.
    live = _equity("2.0", recorded_at="2026-09-09T10:01:00Z")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event_type": "strategy_equity", "payload": live}) + "\n")
    state.on_event("strategy_equity", live)

    assert state._history_thread is not None
    state._history_thread.join(timeout=2)
    assert not state._history_thread.is_alive()

    assert state._pnl_by_strategy["PFOK"] == Decimal("3.0")
    assert state._session_pnl_by_strategy["PFOK"] == Decimal("2.0")
    assert state._history_equity_events == 1
    assert state._bootstrapped is True
