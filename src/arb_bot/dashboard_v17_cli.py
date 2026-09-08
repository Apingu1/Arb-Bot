from __future__ import annotations

import time

from dotenv import load_dotenv

from .config import Settings
from .dashboard_v17 import DashboardServerV17


def cli() -> None:
    load_dotenv()
    settings = Settings()
    server = DashboardServerV17(settings)
    server.start()
    if server._server is None:
        raise SystemExit(1)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
