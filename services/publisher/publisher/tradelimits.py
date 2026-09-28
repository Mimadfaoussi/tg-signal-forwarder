from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import redis.asyncio as aioredis

COOLDOWN_KEY_PREFIX = "publisher:trades:cooldown:"
DAILY_COUNT_KEY_PREFIX = "publisher:trades:daily:"
CHANNEL_DAILY_KEY_PREFIX = "publisher:trades:channel_daily:"
DAILY_KEY_TTL_S = 172_800  # 2 days: generous cleanup buffer, never read after "today"

REASON_COOLDOWN = "cooldown"
REASON_DAILY_CAP = "daily_cap"
REASON_CHANNEL_DAILY_CAP = "channel_daily_cap"


class TradeLimits:
    """Independent of the account-safety RateLimiter (§6.7): this caps how
    many trades get forwarded per calendar day, and blocks re-entering a pair
    that was already forwarded within the cooldown window -- even if that
    earlier trade has already closed. A per-source-channel daily cap keeps one
    chatty channel from using up the whole day's allowance. Each check can be
    disabled by setting its value to 0.

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
        max_per_channel_per_day: int = 0,
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._r = r
        self._max_per_day = max_per_day
        self._cooldown_seconds = int(cooldown_hours * 3600)
        self._max_per_channel_per_day = max_per_channel_per_day
        self._clock = clock

    async def check(self, pair: str | None, source_chat_id: int | None = None) -> str | None:
        """Returns None if forwarding is allowed, else a skip reason."""
        if self._max_per_day > 0:
            count = await self._r.get(self._daily_key())
            if count is not None and int(count) >= self._max_per_day:
                return REASON_DAILY_CAP

        if self._max_per_channel_per_day > 0 and source_chat_id is not None:
            count = await self._r.get(self._channel_daily_key(source_chat_id))
            if count is not None and int(count) >= self._max_per_channel_per_day:
                return REASON_CHANNEL_DAILY_CAP

        if pair and self._cooldown_seconds > 0:
            on_cooldown = await self._r.exists(f"{COOLDOWN_KEY_PREFIX}{pair}")
            if on_cooldown:
                return REASON_COOLDOWN

        return None

    async def record(self, pair: str | None, source_chat_id: int | None = None) -> None:
        """Call once a signal has actually been forwarded."""
        if self._max_per_day > 0:
            key = self._daily_key()
            await self._r.incr(key)
            await self._r.expire(key, DAILY_KEY_TTL_S)

        if self._max_per_channel_per_day > 0 and source_chat_id is not None:
            key = self._channel_daily_key(source_chat_id)
            await self._r.incr(key)
            await self._r.expire(key, DAILY_KEY_TTL_S)

        if pair and self._cooldown_seconds > 0:
            await self._r.set(f"{COOLDOWN_KEY_PREFIX}{pair}", "1", ex=self._cooldown_seconds)

    def _daily_key(self) -> str:
        today = self._clock().date().isoformat()
        return f"{DAILY_COUNT_KEY_PREFIX}{today}"

    def _channel_daily_key(self, source_chat_id: int) -> str:
        today = self._clock().date().isoformat()
        return f"{CHANNEL_DAILY_KEY_PREFIX}{today}:{source_chat_id}"
