from __future__ import annotations

import asyncio
import sys
from typing import Any

import jinja2
from pydantic import ValidationError
from signal_shared.logging import get_logger
from signal_shared.models import Signal
from signal_shared.settings import describe_config_error
from signal_shared.telegram import ChatPermission, describe_chat_permissions, make_client
from telethon import TelegramClient
from telethon.errors import (
    ChannelPrivateError,
    ChatRestrictedError,
    ChatWriteForbiddenError,
    PeerIdInvalidError,
    UserBannedInChannelError,
)

from publisher.entities import rebuild_entities

PERMANENT_ERRORS: tuple[type[Exception], ...] = (
    ChatWriteForbiddenError,
    UserBannedInChannelError,
    ChatRestrictedError,
    ChannelPrivateError,
    PeerIdInvalidError,
)


class TargetNotWritable(Exception):
    def __init__(self, permission: ChatPermission) -> None:
        self.permission = permission
        super().__init__(f"target not writable: {permission.reason}")


async def preflight(client: TelegramClient, target_chat: str, log: Any) -> ChatPermission:
    """§6.5 step 3: resolve TARGET_CHAT and confirm the account can write there.

    Raises TargetNotWritable (caller maps this to exit code 4) if not.
    """
    try:
        entity = await client.get_entity(target_chat)
    except Exception as exc:  # noqa: BLE001
        permission = ChatPermission(
            chat_type="unknown", can_send=False, reason=f"unresolvable: {exc}"
        )
        log.error("target_not_writable", target_chat=target_chat, reason=permission.reason)
        raise TargetNotWritable(permission) from exc

    permission = await describe_chat_permissions(client, entity)
    if not permission.can_send:
        log.error(
            "target_not_writable",
            target_chat=target_chat,
            chat_type=permission.chat_type,
            reason=permission.reason,
        )
        raise TargetNotWritable(permission)

    log.info(
        "target_ok",
        target_chat=target_chat,
        chat_type=permission.chat_type,
        slow_mode_delay=permission.slow_mode_delay,
    )
    return permission


def render_message(
    signal: Signal, *, output_mode: str, account_is_premium: bool, template_dir: str
) -> tuple[str, list[Any] | None, str | None]:
    """Returns (text, formatting_entities, parse_mode)."""
    if output_mode == "template":
        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(template_dir),
            trim_blocks=True,
            lstrip_blocks=True,
            autoescape=jinja2.select_autoescape(default=True),
        )
        template = env.get_template("signal.txt.j2")
        text = template.render(signal=signal).strip()
        return text, None, "html"

    entities = rebuild_entities(signal.raw_entities, account_is_premium=account_is_premium)
    return signal.raw_text, entities, None


async def send_signal(
    client: TelegramClient,
    *,
    target: Any,
    target_topic_id: int | None,
    text: str,
    entities: list[Any] | None,
    parse_mode: str | None,
) -> Any:
    return await client.send_message(
        target,
        text,
        formatting_entities=entities,
        parse_mode=parse_mode,
        reply_to=target_topic_id,
        link_preview=False,
    )


async def _check_target_cli() -> None:
    """`make check-target`: run only the target preflight (§6.5 step 3) and exit.

    Assumes the publisher session is already authorized; does not log in.
    """
    from publisher.settings import PublisherSettings

    try:
        settings = PublisherSettings()  # type: ignore[call-arg]
    except ValidationError as exc:
        print(f"config_error: {describe_config_error(exc)}", file=sys.stderr)
        sys.exit(1)

    client = make_client(
        settings.session_path, settings.tg_api_id, settings.tg_api_hash, "signal-publisher"
    )
    await client.connect()
    try:
        if not await client.is_user_authorized():
            print(
                "Publisher session not authorized. Run 'make login-publisher' first.",
                file=sys.stderr,
            )
            sys.exit(2)
        try:
            permission = await preflight(client, settings.target_chat, get_logger())
        except TargetNotWritable as exc:
            print(f"NOT WRITABLE: {exc.permission.reason}", file=sys.stderr)
            sys.exit(4)
        print(f"OK: account can send to {settings.target_chat} ({permission.chat_type})")
    finally:
        await client.disconnect()  # type: ignore[func-returns-value]


if __name__ == "__main__":
    asyncio.run(_check_target_cli())
