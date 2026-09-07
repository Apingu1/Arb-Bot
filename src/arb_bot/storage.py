from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable


EventListener = Callable[[str, Any], None]


class JsonlRecorder:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._listeners: list[EventListener] = []

    @staticmethod
    def _default(value: Any) -> Any:
        if isinstance(value, Decimal):
            return str(value)
        if is_dataclass(value):
            return asdict(value)
        raise TypeError(f"Cannot JSON encode {type(value)!r}")

    def subscribe(self, listener: EventListener) -> None:
        """Subscribe an in-process observer without changing persisted JSONL semantics."""
        if listener not in self._listeners:
            self._listeners.append(listener)

    def unsubscribe(self, listener: EventListener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def write(self, event_type: str, payload: Any) -> None:
        row = {"event_type": event_type, "payload": payload}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, default=self._default, separators=(",", ":")) + "\n")
        for listener in tuple(self._listeners):
            try:
                listener(event_type, payload)
            except Exception:
                # Dashboard/listener failures must never interrupt the research recorder.
                continue
