from __future__ import annotations

from uuid import uuid4


# One process-wide identifier shared by all Phase 1.8.6 latency-first events.
PHASE186_RUN_ID = uuid4().hex[:12]
