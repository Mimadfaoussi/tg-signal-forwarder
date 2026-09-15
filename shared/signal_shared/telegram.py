from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from telethon import TelegramClient
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import Channel, ChannelParticipantBanned, Chat, User

APP_VERSION = "1.1.0"


def make_client(session_path: str, api_id: int, api_hash: str, device_model: str) -> TelegramClient:
    return TelegramClient(
        session_path,
        api_id,
        api_hash,
        device_model=device_model,
        system_version="Linux",
        app_version=APP_VERSION,
    )


@dataclass
class ChatPermission:
    chat_type: str
    can_send: bool
    reason: str | None = None
    slow_mode_delay: int | None = None


async def describe_chat_permissions(client: TelegramClient, entity: Any) -> ChatPermission:
    """Determine whether `client`'s account can send messages to `entity`.

    Covers the target kinds named in §3/§6.5: a broadcast channel (needs admin
    post rights), a group/supergroup/forum (needs membership and not being
    banned or restricted), and a private chat with a user or bot (needs the
    account to not have blocked them, per the operator's explicit instruction
    that a bot the operator has already started is a valid target).
    """
    if isinstance(entity, User):
        chat_type = "bot" if entity.bot else "user"
        try:
            full = await client(GetFullUserRequest(entity))
        except Exception as exc:  # noqa: BLE001 - any resolution failure means "can't use this target"
            return ChatPermission(
                chat_type=chat_type, can_send=False, reason=f"unresolvable: {exc}"
            )
        blocked = bool(getattr(full.full_user, "blocked", False))
        if blocked:
            return ChatPermission(chat_type=chat_type, can_send=False, reason="blocked_by_account")
        return ChatPermission(chat_type=chat_type, can_send=True)

    if isinstance(entity, Chat):
        if entity.left or getattr(entity, "deactivated", False):
            return ChatPermission(chat_type="group", can_send=False, reason="not_a_member")
        default_banned = getattr(entity, "default_banned_rights", None)
        if default_banned is not None and default_banned.send_messages:
            return ChatPermission(
                chat_type="group", can_send=False, reason="restricted_from_sending"
            )
        return ChatPermission(chat_type="group", can_send=True)

    if isinstance(entity, Channel):
        if not entity.megagroup:
            admin_rights = getattr(entity, "admin_rights", None)
            if entity.creator or (admin_rights and admin_rights.post_messages):
                return ChatPermission(chat_type="channel", can_send=True)
            return ChatPermission(
                chat_type="channel", can_send=False, reason="broadcast_channel_requires_admin"
            )

        chat_type = "forum" if getattr(entity, "forum", False) else "supergroup"
        if entity.left:
            return ChatPermission(chat_type=chat_type, can_send=False, reason="not_a_member")

        try:
            perms = await client.get_permissions(entity, "me")
        except Exception as exc:  # noqa: BLE001
            return ChatPermission(
                chat_type=chat_type, can_send=False, reason=f"unresolvable: {exc}"
            )

        if perms is None:
            return ChatPermission(chat_type=chat_type, can_send=False, reason="unresolvable")

        if perms.has_left:
            return ChatPermission(chat_type=chat_type, can_send=False, reason="not_a_member")

        if perms.is_banned:
            participant = perms.participant
            banned_rights = getattr(participant, "banned_rights", None)
            if isinstance(participant, ChannelParticipantBanned) and banned_rights is not None:
                if not banned_rights.send_messages:
                    slow_mode = getattr(entity, "slowmode_seconds", None)
                    return ChatPermission(
                        chat_type=chat_type, can_send=True, slow_mode_delay=slow_mode
                    )
            return ChatPermission(
                chat_type=chat_type, can_send=False, reason="restricted_from_sending"
            )

        if perms.is_admin or perms.is_creator:
            slow_mode = getattr(entity, "slowmode_seconds", None)
            return ChatPermission(chat_type=chat_type, can_send=True, slow_mode_delay=slow_mode)

        default_banned = getattr(entity, "default_banned_rights", None)
        if default_banned is not None and default_banned.send_messages:
            return ChatPermission(
                chat_type=chat_type, can_send=False, reason="restricted_from_sending"
            )

        slow_mode = getattr(entity, "slowmode_seconds", None)
        return ChatPermission(chat_type=chat_type, can_send=True, slow_mode_delay=slow_mode)

    return ChatPermission(chat_type="unknown", can_send=False, reason="unresolvable")
