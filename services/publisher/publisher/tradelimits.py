from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import redis.asyncio as aioredis

COOLDOWN_KEY_PREFIX = "publisher:trades:cooldown:"
DAILY_COUNT_KEY_PREFIX = "publisher:trades:daily:"
DAILY_KEY_TTL_S = 172_800  # 2 days: generous cleanup buffer, never read after "today"

REASON_COOLDOWN = "cooldown"
REASON_DAILY_CAP = "daily_cap"


class TradeLimits:
    """Independent of the account-safety RateLimiter (§6.7): this caps how
    many trades get forwarded per calendar day, and blocks re-entering a pair
    that was already forwarded within the cooldown window -- even if that
    earlier trade has already closed. Either check can be disabled by setting
    its value to 0.

    State lives in Redis so it survives restarts, same reasoning as the rate
    limiter. The calendar day is taken from `clock()` (default: the
    container's local time, which follows the TZ env var already configured
    for the stack), injectable for tests.
    """

    def __init__(
        self,
        r: aioredis.Redis,
        *,
        max_per_day: int,
        cooldown_hours: float,
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._r = r
        self._max_per_day = max_per_day
        self._cooldown_seconds = int(cooldown_hours * 3600)
        self._clock = clock

    async def check(self, pair: str | None) -> str | None:
        """Returns None if forwarding is allowed, else a skip reason."""
        if self._max_per_day > 0:
            count = await self._r.get(self._daily_key())
            if count is not None and int(count) >= self._max_per_day:
                return REASON_DAILY_CAP

        if pair and self._cooldown_seconds > 0:
            on_cooldown = await self._r.exists(f"{COOLDOWN_KEY_PREFIX}{pair}")
            if on_cooldown:
                return REASON_COOLDOWN

        return None

    async def record(self, pair: str | None) -> None:
        """Call once a signal has actually been forwarded."""
        if self._max_per_day > 0:
            key = self._daily_key()
            await self._r.incr(key)
            await self._r.expire(key, DAILY_KEY_TTL_S)

        if pair and self._cooldown_seconds > 0:
            await self._r.set(f"{COOLDOWN_KEY_PREFIX}{pair}", "1", ex=self._cooldown_seconds)

    def _daily_key(self) -> str:
        today = self._clock().date().isoformat()
        return f"{DAILY_COUNT_KEY_PREFIX}{today}"
