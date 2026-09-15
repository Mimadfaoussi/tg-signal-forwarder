from __future__ import annotations

import asyncio
import sys

from pydantic import ValidationError
from signal_shared.login import run_interactive_login
from signal_shared.settings import describe_config_error
from signal_shared.telegram import describe_chat_permissions, make_client

from publisher.settings import PublisherSettings


async def async_main() -> None:
    try:
        settings = PublisherSettings()  # type: ignore[call-arg]
    except ValidationError as exc:
        print(f"config_error: {describe_config_error(exc)}", file=sys.stderr)
        sys.exit(1)

    await run_interactive_login(
        session_path=settings.session_path,
        api_id=settings.tg_api_id,
        api_hash=settings.tg_api_hash,
        device_model="signal-publisher",
    )

    client = make_client(
        settings.session_path, settings.tg_api_id, settings.tg_api_hash, "signal-publisher"
    )
    await client.connect()
    try:
        entity = await client.get_entity(settings.target_chat)
        permission = await describe_chat_permissions(client, entity)
        status = (
            "CAN send here" if permission.can_send else f"CANNOT send here ({permission.reason})"
        )
        print(f"\nTarget preflight: {settings.target_chat} is a {permission.chat_type} — {status}")
    except Exception as exc:  # noqa: BLE001
        print(f"\nTarget preflight: could not resolve TARGET_CHAT ({exc})")
    finally:
        await client.disconnect()  # type: ignore[func-returns-value]


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
