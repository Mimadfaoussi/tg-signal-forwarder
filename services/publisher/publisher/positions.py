from __future__ import annotations

import re
import time
from collections.abc import Callable

import redis.asyncio as aioredis

POSITIONS_KEY = "publisher:positions:open"
POSITION_MAX_AGE_S = 7 * 24 * 3600  # safety net: auto-release if never closed

# The execution bot's log messages reference pairs without a slash
# ("ADAUSDT"), while our own Signal.pair has one ("ADA/USDT") -- normalize
# both to the same form before comparing.
_NON_ALNUM_RE = re.compile(r"[^A-Z0-9]")

_SL_HIT_RE = re.compile(r"Stop Loss hit for\s*([A-Za-z0-9]+)!", re.IGNORECASE)
_CANCELLED_RE = re.compile(r"([A-Za-z0-9]+)\s*trade\s*#\d+\s*cancelled", re.IGNORECASE)
_TRADE_FAILED_RE = re.compile(r"([A-Za-z0-9]+)\s*trade failed:", re.IGNORECASE)
# Only a *final* TP-hit message contains "All TPs filled" -- an intermediate
# one (TP1 of 5, say) does not, and must NOT release the slot.
_TP_HIT_RE = re.compile(r"TP\d+\s*hit\s*for\s*([A-Za-z0-9]+)!", re.IGNORECASE)
_ALL_TPS_FILLED_RE = re.compile(r"all\s*tps\s*filled", re.IGNORECASE)


def normalize_pair(pair: str) -> str:
    return _NON_ALNUM_RE.sub("", pair.upper())


def parse_close_event(text: str | None) -> str | None:
    """If `text` is one of the execution bot's "position closed" log
    messages, returns the normalized pair to release. Otherwise None --
    covers "New Signal Detected", "Entry filled", the "stop moved" TP
    notification, and anything else we don't specifically recognize.
    """
    if not text:
        return None

    match = _SL_HIT_RE.search(text)
    if match:
        return normalize_pair(match.group(1))

    match = _CANCELLED_RE.search(text)
    if match:
        return normalize_pair(match.group(1))

    match = _TRADE_FAILED_RE.search(text)
    if match:
        return normalize_pair(match.group(1))

    match = _TP_HIT_RE.search(text)
    if match and _ALL_TPS_FILLED_RE.search(text):
        return normalize_pair(match.group(1))

    return None


class PositionTracker:
    """Tracks which pairs currently occupy a "concurrent trade" slot, backed
    by a Redis sorted set (member=normalized pair, score=opened_at) so the
    safety-net max-age prune reuses the same pattern as RateLimiter's hourly
    window.
    """

    def __init__(
        self,
        r: aioredis.Redis,
        *,
        max_concurrent: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._r = r
        self._max_concurrent = max_concurrent
        self._clock = clock

    async def is_at_capacity(self) -> bool:
        if self._max_concurrent <= 0:
            return False
        await self._prune()
        count = await self._r.zcard(POSITIONS_KEY)
        return count >= self._max_concurrent

    async def open(self, pair: str) -> None:
        await self._r.zadd(POSITIONS_KEY, {normalize_pair(pair): self._clock()})

    async def close(self, pair: str) -> None:
        await self._r.zrem(POSITIONS_KEY, normalize_pair(pair))

    async def _prune(self) -> None:
        cutoff = self._clock() - POSITION_MAX_AGE_S
        await self._r.zremrangebyscore(POSITIONS_KEY, 0, cutoff)
