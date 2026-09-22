from datetime import datetime

import pytest
import pytest_asyncio
from fakeredis import aioredis as fakeaioredis
from publisher.tradelimits import REASON_COOLDOWN, REASON_DAILY_CAP, TradeLimits


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


@pytest_asyncio.fixture
async def redis():
    r = fakeaioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


def make_limits(redis, clock, *, max_per_day=3, cooldown_hours=24.0):
    return TradeLimits(redis, max_per_day=max_per_day, cooldown_hours=cooldown_hours, clock=clock)


@pytest.mark.asyncio
async def test_first_trade_for_a_pair_is_allowed(redis) -> None:
    clock = FakeClock(datetime(2026, 9, 22, 10, 0))
    limits = make_limits(redis, clock)

    assert await limits.check("SAGA/USDT") is None


@pytest.mark.asyncio
async def test_cooldown_blocks_re_entry_for_the_same_pair(redis) -> None:
    clock = FakeClock(datetime(2026, 9, 22, 10, 0))
    limits = make_limits(redis, clock, cooldown_hours=24.0)

    await limits.record("PHA/USDT")

    assert await limits.check("PHA/USDT") == REASON_COOLDOWN
    assert await limits.check("SAGA/USDT") is None  # a different pair is unaffected


@pytest.mark.asyncio
async def test_cooldown_expires_after_the_configured_hours(redis) -> None:
    r = redis
    clock = FakeClock(datetime(2026, 9, 22, 10, 0))
    limits = make_limits(r, clock, cooldown_hours=1.0)

    await limits.record("PHA/USDT")
    assert await limits.check("PHA/USDT") == REASON_COOLDOWN

    # fakeredis honors real TTLs against wall-clock time, not our fake clock,
    # so expire the key directly to simulate the cooldown having elapsed.
    await r.delete("publisher:trades:cooldown:PHA/USDT")

    assert await limits.check("PHA/USDT") is None


@pytest.mark.asyncio
async def test_cooldown_disabled_when_zero(redis) -> None:
    clock = FakeClock(datetime(2026, 9, 22, 10, 0))
    limits = make_limits(redis, clock, cooldown_hours=0)

    await limits.record("PHA/USDT")

    assert await limits.check("PHA/USDT") is None


@pytest.mark.asyncio
async def test_pair_none_skips_cooldown_but_not_daily_cap(redis) -> None:
    clock = FakeClock(datetime(2026, 9, 22, 10, 0))
    limits = make_limits(redis, clock, max_per_day=1, cooldown_hours=24.0)

    await limits.record(None)  # e.g. a signal whose pair failed to parse

    assert await limits.check(None) == REASON_DAILY_CAP
    assert await limits.check("SAGA/USDT") == REASON_DAILY_CAP


@pytest.mark.asyncio
async def test_daily_cap_blocks_once_reached(redis) -> None:
    clock = FakeClock(datetime(2026, 9, 22, 10, 0))
    limits = make_limits(redis, clock, max_per_day=2, cooldown_hours=0)

    await limits.record("A/USDT")
    assert await limits.check("B/USDT") is None
    await limits.record("B/USDT")

    assert await limits.check("C/USDT") == REASON_DAILY_CAP


@pytest.mark.asyncio
async def test_daily_cap_resets_on_a_new_calendar_day(redis) -> None:
    clock = FakeClock(datetime(2026, 9, 22, 23, 0))
    limits = make_limits(redis, clock, max_per_day=1, cooldown_hours=0)

    await limits.record("A/USDT")
    assert await limits.check("B/USDT") == REASON_DAILY_CAP

    clock.now = datetime(2026, 9, 23, 0, 5)  # next day
    assert await limits.check("B/USDT") is None


@pytest.mark.asyncio
async def test_daily_cap_disabled_when_zero(redis) -> None:
    clock = FakeClock(datetime(2026, 9, 22, 10, 0))
    limits = make_limits(redis, clock, max_per_day=0, cooldown_hours=0)

    for i in range(10):
        await limits.record(f"PAIR{i}/USDT")

    assert await limits.check("ANOTHER/USDT") is None


@pytest.mark.asyncio
async def test_daily_cap_checked_before_cooldown(redis) -> None:
    # Both could independently block; daily_cap is the more informative one
    # to see first if a pair is also within its cooldown.
    clock = FakeClock(datetime(2026, 9, 22, 10, 0))
    limits = make_limits(redis, clock, max_per_day=1, cooldown_hours=24.0)

    await limits.record("PHA/USDT")

    assert await limits.check("PHA/USDT") == REASON_DAILY_CAP
