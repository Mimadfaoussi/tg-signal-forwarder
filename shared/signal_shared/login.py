from __future__ import annotations

import os
from pathlib import Path

from signal_shared.telegram import describe_chat_permissions, make_client


async def run_interactive_login(
    *, session_path: str, api_id: int, api_hash: str, device_model: str
) -> None:
    """Interactive Telethon login (phone, code, optional 2FA password).

    Writes the session to `session_path`, chmods it 600 (NFR-4), then prints
    the account and its 30 most recent dialogs (including each one's
    @username, when it has one) so the operator can pick SOURCE_CHAT /
    TARGET_CHAT.
    """
    Path(session_path).parent.mkdir(parents=True, exist_ok=True)

    client = make_client(session_path, api_id, api_hash, device_model)
    await client.start()

    session_file = Path(session_path)
    if session_file.exists():
        os.chmod(session_file, 0o600)

    me = await client.get_me()
    username = getattr(me, "username", None)
    print(f"Logged in as {username or me.id} (id={me.id}), device={device_model}")

    print("\nMost recent dialogs (id<TAB>type<TAB>title<TAB>username<TAB>can_send):")
    async for dialog in client.iter_dialogs(limit=30):
        try:
            perm = await describe_chat_permissions(client, dialog.entity)
            chat_type, can_send = perm.chat_type, "yes" if perm.can_send else "no"
        except Exception:  # noqa: BLE001 - listing must not abort on one bad dialog
            chat_type, can_send = "unknown", "no"
        username = getattr(dialog.entity, "username", None)
        # Prefer @username over a bare numeric id for SOURCE_CHAT/TARGET_CHAT: a
        # user/bot id only resolves later if Telethon already cached its
        # access_hash, which isn't guaranteed across separate process runs.
        username_display = f"@{username}" if username else "-"
        print(f"{dialog.id}\t{chat_type}\t{dialog.name}\t{username_display}\t{can_send}")

    await client.disconnect()
