from __future__ import annotations

import redis.asyncio as aioredis
from signal_shared.models import Signal

STREAM = "signals.raw"
MAXLEN = 10000
DEDUPE_TTL_S = 604800


class SignalQueue:
    def __init__(self, r: aioredis.Redis) -> None:
        self._r = r

    async def try_claim(self, signal_id: str) -> bool:
        """Returns True the first time this signal_id is claimed, False if already seen."""
        claimed = await self._r.set(f"dedupe:{signal_id}", "1", nx=True, ex=DEDUPE_TTL_S)
        return bool(claimed)

    async def enqueue(self, signal: Signal) -> None:
        await self._r.xadd(
            STREAM,
            {"payload": signal.model_dump_json()},
            maxlen=MAXLEN,
            approximate=True,
        )

    async def get_last_id(self, chat_id: int) -> int | None:
        value = await self._r.get(f"listener:last_id:{chat_id}")
        return int(value) if value is not None else None

    async def set_last_id(self, chat_id: int, message_id: int) -> None:
        await self._r.set(f"listener:last_id:{chat_id}", message_id)
