from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Iterable


DEFAULT_PROFILE: dict[str, set[str]] = {
    "HYBRID-99": {"BTC", "ETH", "BNB"},
    "HYBRID-98": {"BTC", "ETH", "BNB"},
    "HYBRID-97": set(),
    "HYBRID-96": set(),
    "MAKER-99": set(),
    "MAKER-98": set(),
    "MAKER-97": set(),
    "MAKER-96": set(),
    "PMAKER-Q50": set(),
    "PMAKER-Q100": {"BTC", "ETH"},
    "PMAKER-Q250": {"ETH"},
}


@dataclass
class RuntimeControlsV18:
    assets: tuple[str, ...] = ("BTC", "ETH", "HYPE", "BNB", "DOGE", "XRP", "SOL")
    _enabled_assets: dict[str, set[str]] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def configure(self, models: Iterable[str], assets: Iterable[str] | None = None) -> None:
        with self._lock:
            if assets is not None:
                self.assets = tuple(str(asset).upper() for asset in assets)
            for model in models:
                name = str(model)
                if name not in self._enabled_assets:
                    self._enabled_assets[name] = set(DEFAULT_PROFILE.get(name, set())) & set(self.assets)

    def model_enabled(self, model: str) -> bool:
        with self._lock:
            return bool(self._enabled_assets.get(model, set()))

    def enabled_for(self, model: str, asset: str) -> bool:
        with self._lock:
            return str(asset).upper() in self._enabled_assets.get(model, set())

    def set_model(self, model: str, enabled: bool) -> None:
        with self._lock:
            if model not in self._enabled_assets:
                raise KeyError(model)
            self._enabled_assets[model] = set(self.assets) if enabled else set()

    def set_asset(self, model: str, asset: str, enabled: bool) -> None:
        asset = str(asset).upper()
        with self._lock:
            if model not in self._enabled_assets:
                raise KeyError(model)
            if asset not in self.assets:
                raise ValueError(asset)
            if enabled:
                self._enabled_assets[model].add(asset)
            else:
                self._enabled_assets[model].discard(asset)

    def snapshot(self) -> dict:
        with self._lock:
            rows = []
            for model in sorted(self._enabled_assets):
                enabled_assets = sorted(self._enabled_assets[model])
                rows.append({
                    "model": model,
                    "enabled": bool(enabled_assets),
                    "enabled_assets": enabled_assets,
                    "assets": [
                        {"asset": asset, "enabled": asset in self._enabled_assets[model]}
                        for asset in self.assets
                    ],
                })
            return {"assets": list(self.assets), "models": rows}


runtime_controls = RuntimeControlsV18()
