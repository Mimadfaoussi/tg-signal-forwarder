from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import redis.asyncio as aioredis

POSITIONS_KEY = "publisher:positions:open"  # sorted set: pair -> opened_at
SIGNAL_KEY = "publisher:positions:signal"  # hash: pair -> signal_id
PNL_KEY = "publisher:positions:pnl"  # hash: pair -> cumulative P&L (USDT)
POSITION_MAX_AGE_S = 7 * 24 * 3600  # safety net: auto-release if never closed

# The execution bot's log messages reference pairs without a slash
# ("ADAUSDT"), while our own Signal.pair has one ("ADA/USDT") -- normalize
# both to the same form before comparing.
_NON_ALNUM_RE = re.compile(r"[^A-Z0-9]")

_PNL_RE = re.compile(r"P&L\s*:\s*([+-]?[\d.]+)\s*USDT", re.IGNORECASE)
_SL_HIT_RE = re.compile(r"Stop Loss hit for\s*([A-Za-z0-9]+)!", re.IGNORECASE)
_CANCELLED_RE = re.compile(r"([A-Za-z0-9]+)\s*trade\s*#\d+\s*cancelled", re.IGNORECASE)
_TRADE_FAILED_RE = re.compile(r"([A-Za-z0-9]+)\s*trade failed:", re.IGNORECASE)
# Only a *final* TP-hit message contains "All TPs filled" -- an intermediate
# one (TP1 of 5, say) does not, and is a partial fill, not a close.
_TP_HIT_RE = re.compile(r"TP\d+\s*hit\s*for\s*([A-Za-z0-9]+)!", re.IGNORECASE)
_ALL_TPS_FILLED_RE = re.compile(r"all\s*tps\s*filled", re.IGNORECASE)


def normalize_pair(pair: str) -> str:
    return _NON_ALNUM_RE.sub("", pair.upper())


def _extract_pnl(text: str) -> Decimal | None:
    match = _PNL_RE.search(text)
    if not match:
        return None
    try:
        return Decimal(match.group(1))
    except InvalidOperation:
        return None


@dataclass
class ExecutionEvent:
    """One recognized message from the execution bot's log."""

    pair: str  # already normalized
    is_closing: bool
    pnl_usdt: Decimal | None
    reason: str  # "tp_hit" | "all_tps_filled" | "stop_loss" | "cancelled" | "failed"


@dataclass
class ClosedPosition:
    """What to record on the originating signal once a tracked position closes."""

    signal_id: str
    pnl_usdt: Decimal | None
    reason: str


def parse_execution_event(text: str | None) -> ExecutionEvent | None:
    """If `text` is one of the execution bot's status messages, returns the
    parsed event. Otherwise None -- covers "New Signal Detected", "Entry
    filled", the "stop moved" TP notification, and anything else we don't
    specifically recognize.
    """
    if not text:
        return None

    match = _SL_HIT_RE.search(text)
    if match:
        return ExecutionEvent(normalize_pair(match.group(1)), True, _extract_pnl(text), "stop_loss")

    match = _CANCELLED_RE.search(text)
    if match:
        return ExecutionEvent(normalize_pair(match.group(1)), True, _extract_pnl(text), "cancelled")

    match = _TRADE_FAILED_RE.search(text)
    if match:
        return ExecutionEvent(normalize_pair(match.group(1)), True, None, "failed")

    match = _TP_HIT_RE.search(text)
    if match:
        is_final = bool(_ALL_TPS_FILLED_RE.search(text))
        return ExecutionEvent(
            normalize_pair(match.group(1)),
            is_final,
            _extract_pnl(text),
            "all_tps_filled" if is_final else "tp_hit",
        )

    return None


class PositionTracker:
    """Tracks which pairs currently occupy a "concurrent trade" slot, which
    signal opened each one, and the running P&L accumulated across every
    partial fill for it -- backed by Redis so state survives restarts.
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

    async def open(self, pair: str, signal_id: str) -> None:
        normalized = normalize_pair(pair)
        await self._r.zadd(POSITIONS_KEY, {normalized: self._clock()})
        await self._r.hset(SIGNAL_KEY, normalized, signal_id)

    async def apply_event(self, event: ExecutionEvent) -> ClosedPosition | None:
        """Applies a partial or closing execution event to whichever position
        it refers to. Returns the final outcome to record if this event
        closed a position *we* opened; None for a partial fill, an event for
        an untracked pair, or a closing event we have no record of.
        """
        if event.pnl_usdt is not None:
            await self._r.hincrbyfloat(PNL_KEY, event.pair, float(event.pnl_usdt))

        if not event.is_closing:
            return None

        signal_id = await self._r.hget(SIGNAL_KEY, event.pair)
        total_pnl_raw = await self._r.hget(PNL_KEY, event.pair)

        await self._r.zrem(POSITIONS_KEY, event.pair)
        await self._r.hdel(SIGNAL_KEY, event.pair)
        await self._r.hdel(PNL_KEY, event.pair)

        if signal_id is None:
            return None  # a position we didn't open, or already cleaned up

        total_pnl = (
            Decimal(total_pnl_raw).quantize(Decimal("0.00000001"))
            if total_pnl_raw is not None
            else None
        )
        return ClosedPosition(signal_id=signal_id, pnl_usdt=total_pnl, reason=event.reason)

    async def _prune(self) -> None:
        cutoff = self._clock() - POSITION_MAX_AGE_S
        stale = await self._r.zrangebyscore(POSITIONS_KEY, 0, cutoff)
        if not stale:
            return
        await self._r.zrem(POSITIONS_KEY, *stale)
        for pair in stale:
            await self._r.hdel(SIGNAL_KEY, pair)
            await self._r.hdel(PNL_KEY, pair)
