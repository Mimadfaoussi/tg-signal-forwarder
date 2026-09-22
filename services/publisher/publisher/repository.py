from __future__ import annotations

import json
from pathlib import Path

import asyncpg
from signal_shared.models import Signal


async def run_migrations(pool: asyncpg.Pool, migrations_dir: Path) -> None:
    async with pool.acquire() as conn:
        for path in sorted(migrations_dir.glob("*.sql")):
            sql = path.read_text(encoding="utf-8")
            await conn.execute(sql)


class SignalRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def get_status(self, signal_id: str) -> str | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchval("SELECT status FROM signals WHERE signal_id = $1", signal_id)
            return row

    async def _upsert(
        self,
        signal: Signal,
        *,
        status: str,
        attempts: int,
        target_chat_id: int | None = None,
        target_message_id: int | None = None,
        last_error: str | None = None,
        published: bool = False,
    ) -> None:
        entries_json = json.dumps([str(e) for e in signal.entries])
        take_profits_json = json.dumps([tp.model_dump(mode="json") for tp in signal.take_profits])

        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO signals (
                    signal_id, source_chat_id, source_message_id, pair, entries,
                    take_profits, stop, stop_note, signal_date, raw_text, parse_ok,
                    status, target_chat_id, target_message_id, attempts, last_error,
                    received_at, published_at
                ) VALUES (
                    $1, $2, $3, $4, $5::jsonb, $6::jsonb, $7, $8, $9, $10, $11,
                    $12, $13, $14, $15, $16,
                    $17,
                    CASE WHEN $18 THEN now() ELSE NULL END
                )
                ON CONFLICT (signal_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    target_chat_id = COALESCE(EXCLUDED.target_chat_id, signals.target_chat_id),
                    target_message_id = COALESCE(
                        EXCLUDED.target_message_id, signals.target_message_id
                    ),
                    attempts = EXCLUDED.attempts,
                    last_error = EXCLUDED.last_error,
                    published_at = COALESCE(EXCLUDED.published_at, signals.published_at)
                """,
                signal.signal_id,
                signal.source_chat_id,
                signal.source_message_id,
                signal.pair,
                entries_json,
                take_profits_json,
                signal.stop,
                signal.stop_note,
                signal.signal_date,
                signal.raw_text,
                signal.parse_ok,
                status,
                target_chat_id,
                target_message_id,
                attempts,
                last_error,
                signal.received_at,
                published,
            )

    async def record_sent(
        self, signal: Signal, target_chat_id: int, target_message_id: int, *, attempts: int
    ) -> None:
        await self._upsert(
            signal,
            status="sent",
            attempts=attempts,
            target_chat_id=target_chat_id,
            target_message_id=target_message_id,
            published=True,
        )

    async def record_failed(self, signal: Signal, error: str, *, attempts: int) -> None:
        await self._upsert(signal, status="failed", attempts=attempts, last_error=error)

    async def record_dry_run(self, signal: Signal, *, attempts: int = 0) -> None:
        await self._upsert(signal, status="dry_run", attempts=attempts)

    async def record_skipped(self, signal: Signal, *, reason: str) -> None:
        await self._upsert(signal, status="skipped", attempts=0, last_error=reason)
