from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from pydantic import ValidationError
from signal_shared.extract import extract_from_messages
from signal_shared.logging import get_logger
from signal_shared.models import Signal
from signal_shared.settings import describe_config_error
from signal_shared.telegram import make_client
from telethon import utils as telethon_utils

from listener.main import _parse_source_chat
from listener.settings import ListenerSettings

log = get_logger()


async def _run(chat: str, *, limit: int | None, settings: ListenerSettings) -> list[Signal]:
    client = make_client(
        settings.session_path, settings.tg_api_id, settings.tg_api_hash, "signal-extract"
    )
    await client.connect()
    try:
        if not await client.is_user_authorized():
            print(
                "Listener session not authorized. Run 'make login-listener' first.",
                file=sys.stderr,
            )
            sys.exit(2)

        entity = await client.get_entity(_parse_source_chat(chat))
        chat_id = telethon_utils.get_peer_id(entity)

        messages = [m async for m in client.iter_messages(entity, limit=limit)]
        print(f"Scanned {len(messages)} message(s) from {chat}...", file=sys.stderr)

        signals = extract_from_messages(
            messages, chat_id=chat_id, quote_assets=settings.quote_assets_list
        )
        signals.sort(key=lambda s: s.received_at)
        return signals
    finally:
        await client.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract every trade signal from a Telegram channel's history into a JSON file."
    )
    parser.add_argument("--chat", required=True, help="-100xxxxxxxxxx or @username")
    parser.add_argument("--out", required=True, help="output JSON file path")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="only scan the N most recent messages (default: the entire history)",
    )
    args = parser.parse_args()

    try:
        settings = ListenerSettings()  # type: ignore[call-arg]
    except ValidationError as exc:
        print(f"config_error: {describe_config_error(exc)}", file=sys.stderr)
        sys.exit(1)

    signals = asyncio.run(_run(args.chat, limit=args.limit, settings=settings))

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps([s.model_dump(mode="json") for s in signals], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"Extracted {len(signals)} signal(s) from {args.chat} -> {out_path}")


if __name__ == "__main__":
    main()
