from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

import redis.asyncio as aioredis
from pydantic import ValidationError
from signal_shared.classifier import is_signal
from signal_shared.logging import configure_logging, get_logger
from signal_shared.parser import parse_signal
from signal_shared.settings import describe_config_error
from signal_shared.telegram import make_client
from telethon import TelegramClient, events
from telethon import utils as telethon_utils
from telethon.errors import FloodWaitError
from telethon.tl.custom.message import Message

from listener.health import heartbeat_loop
from listener.queue import SignalQueue
from listener.settings import ListenerSettings

log = get_logger()


async def handle_message(
    message: Message,
    *,
    chat_id: int,
    queue: SignalQueue,
    quote_assets: list[str],
) -> None:
    text = message.raw_text or ""

    if not is_signal(text, quote_assets=quote_assets):
        log.debug("message_skipped", chat_id=chat_id, message_id=message.id, preview=text[:80])
        return

    signal_id = f"{chat_id}:{message.id}"

    if not await queue.try_claim(signal_id):
        log.info("duplicate_skipped", signal_id=signal_id)
        return

    raw_entities = [e.to_dict() for e in (message.entities or [])]
    received_at = message.date or datetime.now(UTC)

    signal = parse_signal(
        text,
        signal_id=signal_id,
        source_chat_id=chat_id,
        source_message_id=message.id,
        received_at=received_at,
        raw_entities=raw_entities,
        quote_assets=quote_assets,
    )

    if not signal.parse_ok:
        log.warning("signal_parse_failed", signal_id=signal_id, raw_text=text)

    await queue.enqueue(signal)
    log.info("signal_enqueued", signal_id=signal_id, pair=signal.pair)


async def catch_up(
    client: TelegramClient,
    entity,
    *,
    chat_id: int,
    queue: SignalQueue,
    quote_assets: list[str],
    limit: int,
) -> None:
    last_id = await queue.get_last_id(chat_id)

    while True:
        try:
            messages = [
                m
                async for m in client.iter_messages(
                    entity, limit=limit, min_id=last_id or 0, reverse=True
                )
            ]
            break
        except FloodWaitError as exc:
            log.warning("flood_wait", seconds=exc.seconds, context="catch_up")
            await asyncio.sleep(exc.seconds)

    for message in messages:
        await handle_message(message, chat_id=chat_id, queue=queue, quote_assets=quote_assets)
        await queue.set_last_id(chat_id, message.id)

    if messages:
        log.info("catch_up_complete", chat_id=chat_id, processed=len(messages))


async def async_main() -> None:
    try:
        settings = ListenerSettings()  # type: ignore[call-arg]
    except ValidationError as exc:
        print(f"config_error: {describe_config_error(exc)}", file=sys.stderr)
        sys.exit(1)

    configure_logging("listener", settings.log_level)

    if not Path(settings.session_path).exists():
        log.error(
            "session_not_authorized",
            hint="Run 'make login' to authorize the listener's Telegram user session.",
        )
        sys.exit(2)

    client = make_client(
        settings.session_path, settings.tg_api_id, settings.tg_api_hash, "signal-listener"
    )
    await client.connect()

    if not await client.is_user_authorized():
        log.error(
            "session_not_authorized",
            hint="Run 'make login' to authorize the listener's Telegram user session.",
        )
        sys.exit(2)

    entities = []
    chat_ids = []
    for source_chat in settings.source_chats_list:
        try:
            entity = await client.get_entity(_parse_source_chat(source_chat))
        except Exception as exc:
            log.error("source_chat_unresolvable", source_chat=source_chat, error=str(exc))
            sys.exit(3)
        entities.append(entity)
        # get_peer_id returns the marked id (-100-prefixed for channels, negative
        # for basic groups) matching the id used everywhere else: dedupe keys,
        # signal_id, Postgres rows, and the -100xxxxxxxxxx form operators pass
        # as SOURCE_CHAT.
        chat_ids.append(telethon_utils.get_peer_id(entity))

    r = aioredis.from_url(settings.redis_url, decode_responses=True)
    queue = SignalQueue(r)
    quote_assets = settings.quote_assets_list

    for entity, chat_id in zip(entities, chat_ids, strict=True):
        await catch_up(
            client,
            entity,
            chat_id=chat_id,
            queue=queue,
            quote_assets=quote_assets,
            limit=settings.catchup_limit,
        )

    @client.on(events.NewMessage(chats=entities))
    async def _on_new_message(event: events.NewMessage.Event) -> None:
        chat_id = event.chat_id
        try:
            await handle_message(
                event.message, chat_id=chat_id, queue=queue, quote_assets=quote_assets
            )
            await queue.set_last_id(chat_id, event.message.id)
        except FloodWaitError as exc:
            log.warning("flood_wait", seconds=exc.seconds, context="live")
            await asyncio.sleep(exc.seconds)
        except Exception:
            log.exception("message_handling_error", chat_id=chat_id, message_id=event.message.id)

    log.info("listener_started", source_chats=settings.source_chats_list, chat_ids=chat_ids)

    heartbeat_task = asyncio.create_task(heartbeat_loop())
    try:
        await client.run_until_disconnected()  # type: ignore[func-returns-value]
    finally:
        heartbeat_task.cancel()
        await r.aclose()


def _parse_source_chat(source_chat: str) -> str | int:
    source_chat = source_chat.strip()
    if source_chat.startswith("@"):
        return source_chat
    try:
        return int(source_chat)
    except ValueError:
        return source_chat


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
