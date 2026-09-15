import pytest
import pytest_asyncio
from fakeredis import aioredis as fakeaioredis
from publisher.ratelimit import RateLimiter


class FrozenClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordingSleep:
    def __init__(self, clock: FrozenClock) -> None:
        self.clock = clock
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.clock.advance(seconds)


@pytest_asyncio.fixture
async def redis():
    r = fakeaioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


def make_limiter(redis, clock, sleep, *, min_interval=5.0, jitter=0.0, max_per_hour=30):
    return RateLimiter(
        redis,
        min_interval=min_interval,
        jitter=jitter,
        max_per_hour=max_per_hour,
        clock=clock,
        sleep=sleep,
        rand=lambda a, b: b,  # deterministic: always the max of the jitter range
    )


@pytest.mark.asyncio
async def test_first_send_does_not_wait(redis) -> None:
    clock = FrozenClock()
    sleep = RecordingSleep(clock)
    limiter = make_limiter(redis, clock, sleep)

    await limiter.wait()

    assert sleep.calls == []


@pytest.mark.asyncio
async def test_enforces_minimum_interval(redis) -> None:
    clock = FrozenClock()
    sleep = RecordingSleep(clock)
    limiter = make_limiter(redis, clock, sleep, min_interval=5.0)

    await limiter.wait()
    await limiter.record_send()

    clock.advance(2.0)  # only 2s elapsed, need 5s
    await limiter.wait()

    assert sleep.calls == [3.0]


@pytest.mark.asyncio
async def test_no_wait_once_interval_has_elapsed(redis) -> None:
    clock = FrozenClock()
    sleep = RecordingSleep(clock)
    limiter = make_limiter(redis, clock, sleep, min_interval=5.0)

    await limiter.wait()
    await limiter.record_send()

    clock.advance(10.0)
    await limiter.wait()

    assert sleep.calls == []


@pytest.mark.asyncio
async def test_slow_mode_overrides_shorter_min_interval(redis) -> None:
    clock = FrozenClock()
    sleep = RecordingSleep(clock)
    limiter = make_limiter(redis, clock, sleep, min_interval=5.0)

    await limiter.wait(slow_mode_delay=30)
    await limiter.record_send()

    clock.advance(5.0)  # past MIN_SEND_INTERVAL, but not the 30s slow mode
    await limiter.wait(slow_mode_delay=30)

    assert sleep.calls == [25.0]


@pytest.mark.asyncio
async def test_jitter_is_added_after_the_interval_wait(redis) -> None:
    clock = FrozenClock()
    sleep = RecordingSleep(clock)
    limiter = make_limiter(redis, clock, sleep, min_interval=5.0, jitter=2.0)

    await limiter.wait()  # first send: no interval wait, but jitter still applies

    assert sleep.calls == [2.0]  # rand() is stubbed to always return the max


@pytest.mark.asyncio
async def test_hourly_cap_makes_the_31st_send_wait(redis) -> None:
    clock = FrozenClock()
    sleep = RecordingSleep(clock)
    limiter = make_limiter(redis, clock, sleep, min_interval=0.0, jitter=0.0, max_per_hour=30)

    for _ in range(30):
        await limiter.wait()
        await limiter.record_send()
        clock.advance(1.0)

    sleep.calls.clear()
    await limiter.wait()

    assert sleep.calls  # the 31st send had to wait for the window to free up


@pytest.mark.asyncio
async def test_state_survives_a_restart_via_redis(redis) -> None:
    clock = FrozenClock()
    sleep = RecordingSleep(clock)

    limiter_a = make_limiter(redis, clock, sleep, min_interval=5.0)
    await limiter_a.wait()
    await limiter_a.record_send()

    # "Restart": a brand new RateLimiter instance, same Redis backing store.
    clock.advance(1.0)
    limiter_b = make_limiter(redis, clock, sleep, min_interval=5.0)
    await limiter_b.wait()

    assert sleep.calls == [4.0]  # honors limiter_a's last send, not a fresh state
