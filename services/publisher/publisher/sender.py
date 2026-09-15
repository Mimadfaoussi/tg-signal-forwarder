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
    ChatForwardsRestrictedError,
    ChatRestrictedError,
    ChatWriteForbiddenError,
    PeerIdInvalidError,
    UserBannedInChannelError,
)

PERMANENT_ERRORS: tuple[type[Exception], ...] = (
    ChatWriteForbiddenError,
    UserBannedInChannelError,
    ChatRestrictedError,
    ChannelPrivateError,
    PeerIdInvalidError,
    ChatForwardsRestrictedError,
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


def render_template_message(signal: Signal, *, template_dir: str) -> str:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(template_dir),
        trim_blocks=True,
        lstrip_blocks=True,
        autoescape=jinja2.select_autoescape(default=True),
    )
    template = env.get_template("signal.txt.j2")
    return template.render(signal=signal).strip()


def preview_text(signal: Signal, *, output_mode: str, template_dir: str) -> str:
    """What DRY_RUN logs as the outgoing payload."""
    if output_mode == "template":
        return render_template_message(signal, template_dir=template_dir)
    return signal.raw_text


async def send_signal(
    client: TelegramClient,
    *,
    target: Any,
    target_topic_id: int | None,
    output_mode: str,
    signal: Signal,
    template_dir: str,
) -> Any:
    """Sends the signal to `target` and returns the resulting Message.

    `copy` mode does a real Telegram forward of the original source message
    (kept formatting, no re-authoring) rather than composing a new one.
    `template` mode still composes and sends a brand-new message rendered
    from the parsed fields, since there's no "original" to forward.
    """
    if output_mode == "template":
        text = render_template_message(signal, template_dir=template_dir)
        return await client.send_message(
            target,
            text,
            parse_mode="html",
            reply_to=target_topic_id,
            link_preview=False,
        )

    messages = await client.forward_messages(
        target,
        signal.source_message_id,
        from_peer=signal.source_chat_id,
    )
    return messages[0]


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
