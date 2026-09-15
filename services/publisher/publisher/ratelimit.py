from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any

import redis.asyncio as aioredis

LAST_SEND_KEY = "publisher:ratelimit:last_send"
SENDS_ZSET_KEY = "publisher:ratelimit:sends"
HOUR_S = 3600.0


class RateLimiter:
    """Keeps the sending account's pace human: a minimum gap between sends
    (raised to match a group's slow mode when that's larger), jitter on top,
    and a rolling hourly cap. State lives in Redis so a restart doesn't reset
    it (§6.7, §7.2 `publisher:ratelimit`).
    """

    def __init__(
        self,
        r: aioredis.Redis,
        *,
        min_interval: float,
        jitter: float,
        max_per_hour: int,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rand: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self._r = r
        self._min_interval = min_interval
        self._jitter = jitter
        self._max_per_hour = max_per_hour
        self._clock = clock
        self._sleep = sleep
        self._rand = rand

    async def wait(self, *, slow_mode_delay: int | None = None, log: Any = None) -> None:
        interval = max(self._min_interval, float(slow_mode_delay or 0))

        while True:
            now = self._clock()
            await self._r.zremrangebyscore(SENDS_ZSET_KEY, 0, now - HOUR_S)
            count = await self._r.zcard(SENDS_ZSET_KEY)
            if count < self._max_per_hour:
                break
            if log is not None:
                log.info("hourly_cap_reached", count=count, max_per_hour=self._max_per_hour)
            oldest = await self._r.zrange(SENDS_ZSET_KEY, 0, 0, withscores=True)
            wait_s = (oldest[0][1] + HOUR_S - now) if oldest else 1.0
            await self._sleep(max(wait_s, 1.0))

        last_send = await self._r.get(LAST_SEND_KEY)
        if last_send is not None:
            elapsed = self._clock() - float(last_send)
            remaining = interval - elapsed
            if remaining > 0:
                await self._sleep(remaining)

        if self._jitter > 0:
            await self._sleep(self._rand(0, self._jitter))

    async def record_send(self) -> None:
        now = self._clock()
        await self._r.set(LAST_SEND_KEY, str(now))
        await self._r.zadd(SENDS_ZSET_KEY, {f"{now!r}": now})
