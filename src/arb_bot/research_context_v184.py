from __future__ import annotations

from uuid import uuid4


# One process-wide identifier shared by all Phase 1.8.4 fast-path events.
PHASE184_RUN_ID = uuid4().hex[:12]
