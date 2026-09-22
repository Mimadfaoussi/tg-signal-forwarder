from __future__ import annotations

import asyncio
import json
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncpg
import redis.asyncio as aioredis
from pydantic import ValidationError
from signal_shared.logging import configure_logging, get_logger
from signal_shared.models import Signal
from signal_shared.settings import describe_config_error
from signal_shared.telegram import make_client
from telethon.errors import (
    AuthKeyDuplicatedError,
    AuthKeyUnregisteredError,
    FloodWaitError,
    SessionRevokedError,
    SlowModeWaitError,
)

from publisher.health import heartbeat_loop
from publisher.ratelimit import RateLimiter
from publisher.repository import SignalRepository, run_migrations
from publisher.sender import (
    PERMANENT_ERRORS,
    TargetNotWritable,
    preflight,
    preview_text,
    send_signal,
)
from publisher.settings import PublisherSettings
from publisher.tradelimits import TradeLimits

STREAM = "signals.raw"
DEAD_STREAM = "signals.dead"
GROUP = "publishers"

BACKOFF_SCHEDULE = [2, 4, 8, 16, 32]
MAX_ATTEMPTS = 5
PERMANENT_ERROR_PAUSE_S = 600

CLAIM_INTERVAL_S = 60
CLAIM_IDLE_MS = 120_000

TEMPLATE_DIR = str(Path(__file__).resolve().parent.parent / "templates")

log = get_logger()


class PermanentSendError(Exception):
    """A permanent Telegram error occurred; the caller must pause and redo preflight."""


class AuthRevoked(Exception):
    """The session got logged out from under us; the caller must exit(2)."""


@dataclass
class TargetState:
    entity: Any
    slow_mode_delay: int | None


async def ensure_group(r: aioredis.Redis) -> None:
    try:
        await r.xgroup_create(STREAM, GROUP, id="$", mkstream=True)
    except aioredis.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def process_entry(
    entry_id: str,
    fields: dict[str, str],
    *,
    client: Any,
    target: TargetState,
    r: aioredis.Redis,
    repo: SignalRepository,
    limiter: RateLimiter,
    trade_limits: TradeLimits,
    settings: PublisherSettings,
) -> None:
    raw_payload = fields.get("payload")
    if raw_payload is None:
        await r.xack(STREAM, GROUP, entry_id)
        return

    payload = json.loads(raw_payload)
    try:
        signal = Signal.model_validate(payload)
    except ValidationError as exc:
        log.error("signal_payload_invalid", entry_id=entry_id, error=str(exc))
        await r.xack(STREAM, GROUP, entry_id)
        return

    bound_log = log.bind(signal_id=signal.signal_id, pair=signal.pair)

    status = await repo.get_status(signal.signal_id)
    if status == "sent":
        await r.xack(STREAM, GROUP, entry_id)
        bound_log.info("duplicate_send_skipped")
        return

    skip_reason = await trade_limits.check(signal.pair)
    if skip_reason:
        await repo.record_skipped(signal, reason=skip_reason)
        await r.xack(STREAM, GROUP, entry_id)
        bound_log.info("signal_skipped", reason=skip_reason)
        return

    if settings.dry_run:
        text = preview_text(signal, output_mode=settings.output_mode, template_dir=TEMPLATE_DIR)
        bound_log.info("dry_run_signal", payload=text)
        await repo.record_dry_run(signal)
        await r.xack(STREAM, GROUP, entry_id)
        return

    attempts = 0
    while True:
        await limiter.wait(slow_mode_delay=target.slow_mode_delay, log=bound_log)

        try:
            message = await send_signal(
                client,
                target=target.entity,
                target_topic_id=settings.target_topic_id,
                output_mode=settings.output_mode,
                signal=signal,
                template_dir=TEMPLATE_DIR,
            )
        except (FloodWaitError, SlowModeWaitError) as exc:
            wait_s = exc.seconds + 1
            if exc.seconds > settings.max_flood_wait:
                bound_log.warning("long_flood_wait", seconds=exc.seconds)
            else:
                bound_log.info("flood_wait", seconds=exc.seconds)
            await asyncio.sleep(wait_s)
            continue  # doesn't count as an attempt
        except (AuthKeyUnregisteredError, AuthKeyDuplicatedError, SessionRevokedError) as exc:
            bound_log.error("session_revoked", error=str(exc))
            raise AuthRevoked from exc
        except PERMANENT_ERRORS as exc:
            attempts += 1
            await repo.record_failed(signal, str(exc), attempts=attempts)
            await r.xack(STREAM, GROUP, entry_id)
            await r.xadd(DEAD_STREAM, {"payload": raw_payload})
            bound_log.error("signal_delivery_rejected", error=str(exc))
            raise PermanentSendError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - broad on purpose: any other send failure retries
            attempts += 1
            if attempts >= MAX_ATTEMPTS:
                await repo.record_failed(signal, str(exc), attempts=attempts)
                await r.xack(STREAM, GROUP, entry_id)
                await r.xadd(DEAD_STREAM, {"payload": raw_payload})
                bound_log.error("signal_delivery_failed", error=str(exc), attempts=attempts)
                return
            delay = BACKOFF_SCHEDULE[min(attempts - 1, len(BACKOFF_SCHEDULE) - 1)]
            bound_log.warning("send_retry", error=str(exc), attempt=attempts, delay=delay)
            await asyncio.sleep(delay)
            continue
        else:
            attempts += 1
            await limiter.record_send()
            await trade_limits.record(signal.pair)
            target_chat_id = getattr(message, "chat_id", None)
            await repo.record_sent(signal, target_chat_id, message.id, attempts=attempts)
            await r.xack(STREAM, GROUP, entry_id)
            bound_log.info("signal_published", target_message_id=message.id, attempts=attempts)
            return


async def _handle_permanent_error(
    client: Any, target: TargetState, settings: PublisherSettings
) -> None:
    log.warning("pausing_after_permanent_error", seconds=PERMANENT_ERROR_PAUSE_S)
    await asyncio.sleep(PERMANENT_ERROR_PAUSE_S)
    permission = await preflight(client, settings.target_chat, log)
    target.slow_mode_delay = permission.slow_mode_delay


async def main_loop(
    client: Any,
    target: TargetState,
    r: aioredis.Redis,
    repo: SignalRepository,
    limiter: RateLimiter,
    trade_limits: TradeLimits,
    settings: PublisherSettings,
) -> None:
    consumer = socket.gethostname()
    while True:
        response = await r.xreadgroup(GROUP, consumer, {STREAM: ">"}, count=1, block=5000)
        if not response:
            continue
        for _stream_name, entries in response:
            for entry_id, fields in entries:
                try:
                    await process_entry(
                        entry_id,
                        fields,
                        client=client,
                        target=target,
                        r=r,
                        repo=repo,
                        limiter=limiter,
                        trade_limits=trade_limits,
                        settings=settings,
                    )
                except PermanentSendError:
                    await _handle_permanent_error(client, target, settings)
                except AuthRevoked:
                    raise
                except Exception:  # noqa: BLE001 - keep the loop alive on unexpected errors
                    log.exception("process_entry_error", entry_id=entry_id)


async def claim_loop(
    client: Any,
    target: TargetState,
    r: aioredis.Redis,
    repo: SignalRepository,
    limiter: RateLimiter,
    trade_limits: TradeLimits,
    settings: PublisherSettings,
) -> None:
    consumer = socket.gethostname()
    while True:
        await asyncio.sleep(CLAIM_INTERVAL_S)
        try:
            cursor = "0-0"
            while True:
                cursor, entries, _ = await r.xautoclaim(
                    STREAM, GROUP, consumer, min_idle_time=CLAIM_IDLE_MS, start_id=cursor, count=50
                )
                for entry_id, fields in entries:
                    log.info("entry_reclaimed", entry_id=entry_id)
                    try:
                        await process_entry(
                            entry_id,
                            fields,
                            client=client,
                            target=target,
                            r=r,
                            repo=repo,
                            limiter=limiter,
                            trade_limits=trade_limits,
                            settings=settings,
                        )
                    except PermanentSendError:
                        await _handle_permanent_error(client, target, settings)
                if cursor == "0-0" or not entries:
                    break
        except AuthRevoked:
            raise
        except Exception:  # noqa: BLE001
            log.exception("claim_loop_error")


async def async_main() -> None:
    try:
        settings = PublisherSettings()  # type: ignore[call-arg]
    except ValidationError as exc:
        print(f"config_error: {describe_config_error(exc)}", file=sys.stderr)
        sys.exit(1)

    configure_logging("publisher", settings.log_level)

    if not Path(settings.session_path).exists():
        log.error(
            "session_not_authorized",
            hint="Run 'make login-publisher' to authorize the publisher's Telegram user session.",
        )
        sys.exit(2)

    client = make_client(
        settings.session_path, settings.tg_api_id, settings.tg_api_hash, "signal-publisher"
    )
    await client.connect()

    if not await client.is_user_authorized():
        log.error(
            "session_not_authorized",
            hint="Run 'make login-publisher' to authorize the publisher's Telegram user session.",
        )
        sys.exit(2)

    if settings.output_mode == "copy" and settings.target_topic_id is not None:
        log.warning(
            "target_topic_id_ignored_in_copy_mode",
            hint="Telethon's forward_messages has no forum-topic targeting; the "
            "forward will land in the target's default topic. Use OUTPUT_MODE=template "
            "if you need it to land in a specific forum topic.",
        )

    # Forwarding (copy mode) needs the source chat's entity cached locally so
    # `from_peer=signal.source_chat_id` resolves; this session never otherwise
    # interacts with the source chat, so warm the cache from all chats the
    # account is a member of (get_entity on a bare id fails without this).
    if settings.output_mode == "copy":
        await client.get_dialogs()

    pool = await asyncpg.create_pool(dsn=settings.database_url)
    await run_migrations(pool, Path(__file__).resolve().parent.parent / "migrations")

    # socket_timeout must exceed the BLOCK duration used by xreadgroup below, or the
    # client's socket read times out and raises before Redis has a chance to reply.
    r = aioredis.from_url(settings.redis_url, decode_responses=True, socket_timeout=15.0)
    await ensure_group(r)

    try:
        permission = await preflight(client, settings.target_chat, log)
    except TargetNotWritable:
        await client.disconnect()  # type: ignore[func-returns-value]
        await r.aclose()
        await pool.close()
        sys.exit(4)

    target_entity = await client.get_entity(settings.target_chat)
    target = TargetState(entity=target_entity, slow_mode_delay=permission.slow_mode_delay)

    repo = SignalRepository(pool)
    limiter = RateLimiter(
        r,
        min_interval=settings.min_send_interval,
        jitter=settings.send_jitter,
        max_per_hour=settings.max_sends_per_hour,
    )
    trade_limits = TradeLimits(
        r,
        max_per_day=settings.max_trades_per_day,
        cooldown_hours=settings.pair_cooldown_hours,
    )

    log.info(
        "publisher_started",
        output_mode=settings.output_mode,
        dry_run=settings.dry_run,
        max_trades_per_day=settings.max_trades_per_day,
        pair_cooldown_hours=settings.pair_cooldown_hours,
    )

    tasks = [
        asyncio.create_task(heartbeat_loop()),
        asyncio.create_task(main_loop(client, target, r, repo, limiter, trade_limits, settings)),
        asyncio.create_task(claim_loop(client, target, r, repo, limiter, trade_limits, settings)),
    ]
    try:
        await asyncio.gather(*tasks)
    except AuthRevoked:
        log.error("exiting_for_relogin", hint="Run 'make login-publisher' again.")
        sys.exit(2)
    finally:
        for task in tasks:
            task.cancel()
        await client.disconnect()  # type: ignore[func-returns-value]
        await r.aclose()
        await pool.close()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
