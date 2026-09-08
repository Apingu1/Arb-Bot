from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(slots=True)
class _Episode:
    episode_id: str
    last_seen_monotonic: float


class OutcomeEpisodeClusterer:
    """Cluster correlated maker-family outcomes on one market into one episode."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._last_by_slug: dict[str, _Episode] = {}

    def assign(self, slug: str, *, window_ms: int = 2000) -> str:
        now = time.monotonic()
        window_s = max(0, window_ms) / 1000
        with self._lock:
            prior = self._last_by_slug.get(slug)
            if prior is not None and now - prior.last_seen_monotonic <= window_s:
                prior.last_seen_monotonic = now
                return prior.episode_id

            wall = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            episode_id = f"{slug}:EP:{wall}"
            self._last_by_slug[slug] = _Episode(episode_id, now)
            return episode_id

    def reset(self) -> None:
        with self._lock:
            self._last_by_slug.clear()


outcome_episode_clusterer = OutcomeEpisodeClusterer()
