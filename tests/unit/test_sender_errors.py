import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest
from fakeredis import aioredis as fakeaioredis
from publisher import main as publisher_main
from publisher.main import AuthRevoked, PermanentSendError, TargetState
from signal_shared.models import Signal
from telethon.errors import (
    AuthKeyDuplicatedError,
    ChatForwardsRestrictedError,
    ChatWriteForbiddenError,
    FloodWaitError,
    SlowModeWaitError,
)


class FakeSettings:
    output_mode = "copy"
    dry_run = False
    target_chat = "target"
    target_topic_id = None
    max_flood_wait = 3600


class FakeRedis:
    def __init__(self) -> None:
        self.acked: list[tuple[str, str, str]] = []
        self.dead_letters: list[dict] = []

    async def xack(self, stream: str, group: str, entry_id: str) -> None:
        self.acked.append((stream, group, entry_id))

    async def xadd(self, stream: str, fields: dict) -> None:
        self.dead_letters.append({"stream": stream, "fields": fields})


class FakeRepository:
    def __init__(self, existing_status: str | None = None) -> None:
        self.existing_status = existing_status
        self.sent: list[tuple] = []
        self.failed: list[tuple] = []
        self.dry_runs: list[Signal] = []

    async def get_status(self, signal_id: str) -> str | None:
        return self.existing_status

    async def record_sent(self, signal, target_chat_id, target_message_id, *, attempts):
        self.sent.append((signal, target_chat_id, target_message_id, attempts))

    async def record_failed(self, signal, error, *, attempts):
        self.failed.append((signal, error, attempts))

    async def record_dry_run(self, signal, *, attempts=0):
        self.dry_runs.append(signal)


class FakeLimiter:
    def __init__(self) -> None:
        self.waits = 0
        self.sends = 0

    async def wait(self, *, slow_mode_delay=None, log=None):
        self.waits += 1

    async def record_send(self) -> None:
        self.sends += 1


class FakeMessage:
    def __init__(self, message_id: int, chat_id: int = -100999) -> None:
        self.id = message_id
        self.chat_id = chat_id


def make_signal_fields(signal_id: str = "-1001:1") -> dict:
    signal = Signal(
        signal_id=signal_id,
        source_chat_id=-1001,
        source_message_id=1,
        pair="SAGA/USDT",
        base="SAGA",
        quote="USDT",
        entries=[Decimal("0.01807")],
        take_profits=[],
        stop=Decimal("0.01794"),
        raw_text="#SAGA/USDT\nEntry1: 0.01807\nTP1: 0.01850\nStop: 0.01794\n",
        raw_entities=[],
        received_at=datetime(2026, 9, 14, 12, 0, tzinfo=UTC),
        parse_ok=True,
    )
    return {"payload": signal.model_dump_json()}


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fast_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(publisher_main.asyncio, "sleep", fast_sleep)


def make_client(call_side_effect, *, response_message=None) -> AsyncMock:
    """A fake Telethon client for the raw-API send path: `client(request)` is
    the RPC call (raises/returns per `call_side_effect`), `get_input_entity`
    always succeeds, and `_get_response_message` (sync, called only after a
    successful RPC) returns `response_message` -- a list for forward mode
    (`sent[0]` is used), a single Message for template mode.
    """
    client = AsyncMock(side_effect=call_side_effect)
    client.get_input_entity = AsyncMock(return_value="input-entity")
    client._get_response_message = Mock(return_value=response_message)
    return client


@pytest.mark.asyncio
async def test_process_entry_success() -> None:
    client = make_client(["raw-result"], response_message=[FakeMessage(42)])
    r, repo, limiter = FakeRedis(), FakeRepository(), FakeLimiter()
    target = TargetState(entity="target-entity", slow_mode_delay=None)

    await publisher_main.process_entry(
        "1-0",
        make_signal_fields(),
        client=client,
        target=target,
        r=r,
        repo=repo,
        limiter=limiter,
        settings=FakeSettings(),
    )

    assert len(repo.sent) == 1
    assert repo.sent[0][2] == 42
    assert r.acked == [("signals.raw", "publishers", "1-0")]
    assert r.dead_letters == []
    assert limiter.sends == 1


@pytest.mark.asyncio
async def test_flood_wait_retries_without_counting_as_an_attempt() -> None:
    client = make_client(
        [FloodWaitError(request=None, capture=5), "raw-result"],
        response_message=[FakeMessage(7)],
    )
    r, repo, limiter = FakeRedis(), FakeRepository(), FakeLimiter()
    target = TargetState(entity="target-entity", slow_mode_delay=None)

    await publisher_main.process_entry(
        "1-0",
        make_signal_fields(),
        client=client,
        target=target,
        r=r,
        repo=repo,
        limiter=limiter,
        settings=FakeSettings(),
    )

    assert client.call_count == 2
    assert len(repo.sent) == 1
    assert repo.sent[0][3] == 1  # attempts: the flood wait didn't count


@pytest.mark.asyncio
async def test_slow_mode_wait_retries_same_message() -> None:
    client = make_client(
        [SlowModeWaitError(request=None, capture=10), "raw-result"],
        response_message=[FakeMessage(9)],
    )
    r, repo, limiter = FakeRedis(), FakeRepository(), FakeLimiter()
    target = TargetState(entity="target-entity", slow_mode_delay=None)

    await publisher_main.process_entry(
        "1-0",
        make_signal_fields(),
        client=client,
        target=target,
        r=r,
        repo=repo,
        limiter=limiter,
        settings=FakeSettings(),
    )

    assert client.call_count == 2
    assert len(repo.sent) == 1


@pytest.mark.asyncio
async def test_connection_error_backs_off_then_dead_letters() -> None:
    client = make_client(ConnectionError("boom"))
    r, repo, limiter = FakeRedis(), FakeRepository(), FakeLimiter()
    target = TargetState(entity="target-entity", slow_mode_delay=None)

    await publisher_main.process_entry(
        "1-0",
        make_signal_fields(),
        client=client,
        target=target,
        r=r,
        repo=repo,
        limiter=limiter,
        settings=FakeSettings(),
    )

    assert client.call_count == publisher_main.MAX_ATTEMPTS
    assert len(repo.failed) == 1
    assert r.acked == [("signals.raw", "publishers", "1-0")]
    assert len(r.dead_letters) == 1


@pytest.mark.asyncio
async def test_chat_write_forbidden_does_not_retry_and_raises_permanent_error() -> None:
    client = make_client(ChatWriteForbiddenError(request=None))
    r, repo, limiter = FakeRedis(), FakeRepository(), FakeLimiter()
    target = TargetState(entity="target-entity", slow_mode_delay=None)

    with pytest.raises(PermanentSendError):
        await publisher_main.process_entry(
            "1-0",
            make_signal_fields(),
            client=client,
            target=target,
            r=r,
            repo=repo,
            limiter=limiter,
            settings=FakeSettings(),
        )

    assert client.call_count == 1
    assert len(repo.failed) == 1
    assert r.acked == [("signals.raw", "publishers", "1-0")]
    assert len(r.dead_letters) == 1


@pytest.mark.asyncio
async def test_chat_forwards_restricted_does_not_retry_and_raises_permanent_error() -> None:
    # e.g. the source channel has "Restrict Saving Content" enabled, which
    # blocks forwarding via the API entirely -- no amount of retrying helps.
    client = make_client(ChatForwardsRestrictedError(request=None))
    r, repo, limiter = FakeRedis(), FakeRepository(), FakeLimiter()
    target = TargetState(entity="target-entity", slow_mode_delay=None)

    with pytest.raises(PermanentSendError):
        await publisher_main.process_entry(
            "1-0",
            make_signal_fields(),
            client=client,
            target=target,
            r=r,
            repo=repo,
            limiter=limiter,
            settings=FakeSettings(),
        )

    assert client.call_count == 1
    assert len(repo.failed) == 1
    assert r.acked == [("signals.raw", "publishers", "1-0")]
    assert len(r.dead_letters) == 1


@pytest.mark.asyncio
async def test_auth_key_duplicated_raises_and_leaves_message_pending() -> None:
    client = make_client(AuthKeyDuplicatedError(request=None))
    r, repo, limiter = FakeRedis(), FakeRepository(), FakeLimiter()
    target = TargetState(entity="target-entity", slow_mode_delay=None)

    with pytest.raises(AuthRevoked):
        await publisher_main.process_entry(
            "1-0",
            make_signal_fields(),
            client=client,
            target=target,
            r=r,
            repo=repo,
            limiter=limiter,
            settings=FakeSettings(),
        )

    assert r.acked == []  # never acked: stays pending in the consumer group
    assert r.dead_letters == []
    assert repo.failed == []
    assert repo.sent == []


@pytest.mark.asyncio
async def test_duplicate_signal_id_skips_send() -> None:
    client = make_client(["raw-result"], response_message=[FakeMessage(1)])
    r = FakeRedis()
    repo = FakeRepository(existing_status="sent")
    limiter = FakeLimiter()
    target = TargetState(entity="target-entity", slow_mode_delay=None)

    await publisher_main.process_entry(
        "1-0",
        make_signal_fields(),
        client=client,
        target=target,
        r=r,
        repo=repo,
        limiter=limiter,
        settings=FakeSettings(),
    )

    assert client.call_count == 0
    assert repo.sent == []
    assert r.acked == [("signals.raw", "publishers", "1-0")]


@pytest.mark.asyncio
async def test_same_signal_uses_the_same_random_id_across_retries() -> None:
    # This is the actual fix for "forwarded 5 times": Telegram's server
    # recognizes a repeated random_id and returns the already-created message
    # instead of making a duplicate, but only if every retry supplies the
    # SAME random_id. Confirms our retry loop does that.
    from publisher.sender import _stable_random_id

    seen_random_ids: list[int] = []

    async def capture_random_id(request):
        seen_random_ids.append(request.random_id[0])
        raise ConnectionError("simulated: response lost after real send")

    client = AsyncMock(side_effect=capture_random_id)
    client.get_input_entity = AsyncMock(return_value="input-entity")
    r, repo, limiter = FakeRedis(), FakeRepository(), FakeLimiter()
    target = TargetState(entity="target-entity", slow_mode_delay=None)

    await publisher_main.process_entry(
        "1-0",
        make_signal_fields("-1001:1"),
        client=client,
        target=target,
        r=r,
        repo=repo,
        limiter=limiter,
        settings=FakeSettings(),
    )

    assert len(seen_random_ids) == publisher_main.MAX_ATTEMPTS
    assert len(set(seen_random_ids)) == 1  # every attempt reused the same one
    assert seen_random_ids[0] == _stable_random_id("-1001:1", salt="forward")


@pytest.mark.asyncio
async def test_permanent_error_pause_redoes_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    from signal_shared.telegram import ChatPermission

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(publisher_main.asyncio, "sleep", fake_sleep)

    async def fake_preflight(client, target_chat, log):
        return ChatPermission(chat_type="supergroup", can_send=True, slow_mode_delay=15)

    monkeypatch.setattr(publisher_main, "preflight", fake_preflight)

    target = TargetState(entity="target-entity", slow_mode_delay=None)
    await publisher_main._handle_permanent_error(
        client=None, target=target, settings=FakeSettings()
    )

    assert sleeps == [publisher_main.PERMANENT_ERROR_PAUSE_S]
    assert target.slow_mode_delay == 15


@pytest.mark.asyncio
async def test_claim_loop_reclaims_and_sends_a_pending_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression test: claim_loop crashed every run with
    # "xautoclaim() got an unexpected keyword argument 'start'" (redis-py's
    # actual parameter is `start_id`) -- caught only by running the real
    # stack long enough for the 60s claim interval to fire, since no test
    # exercised claim_loop's xautoclaim call at all before this.
    r = fakeaioredis.FakeRedis(decode_responses=True)
    await r.xadd(publisher_main.STREAM, make_signal_fields())
    await r.xgroup_create(publisher_main.STREAM, publisher_main.GROUP, id="0", mkstream=True)
    # Deliver to a now-dead consumer so the entry sits pending/unacked.
    await r.xreadgroup(
        publisher_main.GROUP, "dead-consumer", {publisher_main.STREAM: ">"}, count=10
    )

    monkeypatch.setattr(publisher_main, "CLAIM_IDLE_MS", 0)

    sleep_calls = 0

    async def fake_sleep(_seconds: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(publisher_main.asyncio, "sleep", fake_sleep)

    client = make_client(["raw-result"], response_message=[FakeMessage(99)])
    repo = FakeRepository()
    limiter = FakeLimiter()
    target = TargetState(entity="target-entity", slow_mode_delay=None)

    with pytest.raises(asyncio.CancelledError):
        await publisher_main.claim_loop(client, target, r, repo, limiter, FakeSettings())

    assert client.call_count == 1
    assert len(repo.sent) == 1
