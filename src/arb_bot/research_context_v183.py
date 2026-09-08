from __future__ import annotations

from uuid import uuid4


# One process-wide identifier shared by all Phase 1.8.3 observational events.
# This lets arb-report isolate a single run without changing historical event
# schemas or strategy behaviour.
PHASE183_RUN_ID = uuid4().hex[:12]
