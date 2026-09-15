from __future__ import annotations

import asyncio
import sys

from pydantic import ValidationError
from signal_shared.login import run_interactive_login
from signal_shared.settings import describe_config_error

from listener.settings import ListenerSettings


async def async_main() -> None:
    try:
        settings = ListenerSettings()  # type: ignore[call-arg]
    except ValidationError as exc:
        print(f"config_error: {describe_config_error(exc)}", file=sys.stderr)
        sys.exit(1)

    await run_interactive_login(
        session_path=settings.session_path,
        api_id=settings.tg_api_id,
        api_hash=settings.tg_api_hash,
        device_model="signal-listener",
    )


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
