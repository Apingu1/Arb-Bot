from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any


class JsonlRecorder:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _default(value: Any) -> Any:
        if isinstance(value, Decimal):
            return str(value)
        if is_dataclass(value):
            return asdict(value)
        raise TypeError(f"Cannot JSON encode {type(value)!r}")

    def write(self, event_type: str, payload: Any) -> None:
        row = {"event_type": event_type, "payload": payload}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, default=self._default, separators=(",", ":")) + "\n")
