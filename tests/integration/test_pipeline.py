"""Integration tests against real Redis and Postgres (started via docker compose),
with the Telegram user-account client (Telethon) mocked.

Run with: make test-integration
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import asyncpg
import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from publisher import main as publisher_main
from publisher.main import TargetState
from publisher.ratelimit import RateLimiter
from publisher.repository import SignalRepository, run_migrations
from signal_shared.parser import parse_signal

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://forwarder:change-me@postgres:5432/forwarder"
)

FIXTURES = Path(__file__).parent.parent / "fixtures"

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "services" / "publisher" / "migrations"


class FakeSettings:
    output_mode = "copy"
    account_is_premium = False
    dry_run = False
    target_chat = "-1009999999"
    target_topic_id = None
    max_flood_wait = 3600


class FakeMessage:
    def __init__(self, message_id: int, chat_id: int = -1009999999) -> None:
        self.id = message_id
        self.chat_id = chat_id


def _make_fields(fixture: str, signal_id: str) -> dict:
    text = (FIXTURES / fixture).read_text(encoding="utf-8")
    chat_id, message_id = signal_id.split(":")
    signal = parse_signal(
        text,
        signal_id=signal_id,
        source_chat_id=int(chat_id),
        source_message_id=int(message_id),
        received_at=datetime.now(UTC),
    )
    return {"payload": signal.model_dump_json()}


def make_client(message_id: int) -> AsyncMock:
    client = AsyncMock()
    client.send_message = AsyncMock(return_value=FakeMessage(message_id))
    return client


def make_limiter(r: aioredis.Redis) -> RateLimiter:
    async def no_sleep(_seconds: float) -> None:
        return None

    return RateLimiter(r, min_interval=0.0, jitter=0.0, max_per_hour=1000, sleep=no_sleep)


@pytest_asyncio.fixture
async def pool():
    p = await asyncpg.create_pool(dsn=DATABASE_URL)
    await run_migrations(p, MIGRATIONS_DIR)
    async with p.acquire() as conn:
        await conn.execute("TRUNCATE TABLE signals")
    yield p
    await p.close()


@pytest_asyncio.fixture
async def r():
    client = aioredis.from_url(REDIS_URL, decode_responses=True, socket_timeout=15.0)
    await client.flushdb()
    try:
        await client.xgroup_create(
            publisher_main.STREAM, publisher_main.GROUP, id="$", mkstream=True
        )
    except aioredis.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise
    yield client
    await client.aclose()


@pytest.mark.asyncio
async def test_signal_is_delivered_once_and_recorded(pool: asyncpg.Pool, r: aioredis.Redis) -> None:
    client = make_client(101)
    entry_id = await r.xadd(publisher_main.STREAM, _make_fields("signal_saga.txt", "-1001:1"))

    repo = SignalRepository(pool)
    limiter = make_limiter(r)
    target = TargetState(entity="target-entity", slow_mode_delay=None)

    response = await r.xreadgroup(
        publisher_main.GROUP, "publisher-1", {publisher_main.STREAM: ">"}, count=1
    )
    [(_, entries)] = response
    [(delivered_id, fields)] = entries
    assert delivered_id == entry_id

    await publisher_main.process_entry(
        delivered_id,
        fields,
        client=client,
        target=target,
        r=r,
        repo=repo,
        limiter=limiter,
        settings=FakeSettings(),
    )

    assert client.send_message.call_count == 1

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, target_message_id FROM signals WHERE signal_id = $1", "-1001:1"
        )
    assert row["status"] == "sent"
    assert row["target_message_id"] == 101

    pending = await r.xpending(publisher_main.STREAM, publisher_main.GROUP)
    assert pending["pending"] == 0


@pytest.mark.asyncio
async def test_restart_mid_flight_does_not_lose_or_duplicate(
    pool: asyncpg.Pool, r: aioredis.Redis
) -> None:
    client = make_client(202)
    consumer = "publisher-restart-test"
    entry_id = await r.xadd(publisher_main.STREAM, _make_fields("signal_gps.txt", "-1002:1"))

    # Simulate a crash: the entry is delivered to a consumer (now pending/unacked)
    # but the process dies before it gets processed at all.
    response = await r.xreadgroup(
        publisher_main.GROUP, consumer, {publisher_main.STREAM: ">"}, count=1
    )
    [(_, entries)] = response
    [(delivered_id, fields)] = entries
    assert delivered_id == entry_id

    repo = SignalRepository(pool)
    limiter = make_limiter(r)
    target = TargetState(entity="target-entity", slow_mode_delay=None)

    # "Restart": a new publisher process with the same consumer name re-reads
    # its own still-pending entries (id "0") rather than only new ones (">").
    recovery = await r.xreadgroup(
        publisher_main.GROUP, consumer, {publisher_main.STREAM: "0"}, count=1
    )
    [(_, recovered_entries)] = recovery
    [(recovered_id, recovered_fields)] = recovered_entries
    assert recovered_id == delivered_id

    await publisher_main.process_entry(
        recovered_id,
        recovered_fields,
        client=client,
        target=target,
        r=r,
        repo=repo,
        limiter=limiter,
        settings=FakeSettings(),
    )

    # Nothing lost: exactly one send happened and the row is marked sent.
    assert client.send_message.call_count == 1
    async with pool.acquire() as conn:
        status = await conn.fetchval("SELECT status FROM signals WHERE signal_id = $1", "-1002:1")
    assert status == "sent"

    # A further redelivery of the same (already-acked) entry must not resend.
    await publisher_main.process_entry(
        recovered_id,
        recovered_fields,
        client=client,
        target=target,
        r=r,
        repo=repo,
        limiter=limiter,
        settings=FakeSettings(),
    )
    assert client.send_message.call_count == 1

    pending = await r.xpending(publisher_main.STREAM, publisher_main.GROUP)
    assert pending["pending"] == 0
