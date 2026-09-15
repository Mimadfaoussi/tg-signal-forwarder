from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from publisher import main as publisher_main
from publisher.main import AuthRevoked, PermanentSendError, TargetState
from signal_shared.models import Signal
from telethon.errors import (
    AuthKeyDuplicatedError,
    ChatWriteForbiddenError,
    FloodWaitError,
    SlowModeWaitError,
)


class FakeSettings:
    output_mode = "copy"
    account_is_premium = False
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


def make_client(send_side_effect) -> AsyncMock:
    client = AsyncMock()
    client.send_message = AsyncMock(side_effect=send_side_effect)
    return client


@pytest.mark.asyncio
async def test_process_entry_success() -> None:
    client = make_client([FakeMessage(42)])
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
    client = make_client([FloodWaitError(request=None, capture=5), FakeMessage(7)])
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

    assert client.send_message.call_count == 2
    assert len(repo.sent) == 1
    assert repo.sent[0][3] == 1  # attempts: the flood wait didn't count


@pytest.mark.asyncio
async def test_slow_mode_wait_retries_same_message() -> None:
    client = make_client([SlowModeWaitError(request=None, capture=10), FakeMessage(9)])
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

    assert client.send_message.call_count == 2
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

    assert client.send_message.call_count == publisher_main.MAX_ATTEMPTS
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

    assert client.send_message.call_count == 1
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
    client = make_client([FakeMessage(1)])
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

    assert client.send_message.call_count == 0
    assert repo.sent == []
    assert r.acked == [("signals.raw", "publishers", "1-0")]


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
