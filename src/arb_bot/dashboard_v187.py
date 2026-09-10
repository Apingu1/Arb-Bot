from __future__ import annotations

from .dashboard_v186 import DashboardServerV186, DashboardStateV186


class DashboardStateV187(DashboardStateV186):
    """1.8.7 dashboard state with explicit BFOK family attribution."""

    def publish(self, *args, **kwargs):
        state = super().publish(*args, **kwargs)
        for row in state.get("strategies", []):
            if str(row.get("strategy") or "").startswith("BFOK-"):
                row["family"] = "BFOK"
        for row in state.get("asset_strategy_rows", []):
            if str(row.get("strategy") or "").startswith("BFOK-"):
                row["family"] = "BFOK"
        for row in state.get("session_explorer", []):
            if str(row.get("strategy") or "").startswith("BFOK-"):
                row["family"] = "BFOK"
        with self._lock:
            self._state = state
        return state


DashboardServerV187 = DashboardServerV186
