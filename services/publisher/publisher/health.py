from __future__ import annotations

import asyncio
from pathlib import Path

HEALTH_FILE = Path("/tmp/healthy")
HEARTBEAT_INTERVAL_S = 30


async def heartbeat_loop() -> None:
    while True:
        HEALTH_FILE.touch()
        await asyncio.sleep(HEARTBEAT_INTERVAL_S)
