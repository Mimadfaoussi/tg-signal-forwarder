from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from signal_shared.extract import extract_from_messages, serialize_messages
from signal_shared.logging import get_logger
from signal_shared.settings import describe_config_error
from signal_shared.telegram import make_client
from telethon import utils as telethon_utils

from listener.main import _parse_source_chat
from listener.settings import ListenerSettings

log = get_logger()


async def _run(
    chat: str, *, limit: int | None, mode: str, settings: ListenerSettings
) -> list[dict[str, Any]]:
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

        if mode == "full":
            records = serialize_messages(messages)
            records.sort(key=lambda r: r["date"] or "")
            return records

        signals = extract_from_messages(
            messages, chat_id=chat_id, quote_assets=settings.quote_assets_list
        )
        signals.sort(key=lambda s: s.received_at)
        return [s.model_dump(mode="json") for s in signals]
    finally:
        await client.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract a Telegram channel's history into a JSON file: either just "
        "the recognized trade signals, or every message, unfiltered."
    )
    parser.add_argument("--chat", required=True, help="-100xxxxxxxxxx or @username")
    parser.add_argument("--out", required=True, help="output JSON file path")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="only scan the N most recent messages (default: the entire history)",
    )
    parser.add_argument(
        "--mode",
        choices=["signals", "full"],
        default="signals",
        help="'signals' (default): only messages classified as trade signals, parsed into "
        "fields. 'full': every message in the chat, unfiltered and unparsed.",
    )
    args = parser.parse_args()

    try:
        settings = ListenerSettings()  # type: ignore[call-arg]
    except ValidationError as exc:
        print(f"config_error: {describe_config_error(exc)}", file=sys.stderr)
        sys.exit(1)

    records = asyncio.run(_run(args.chat, limit=args.limit, mode=args.mode, settings=settings))

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(records, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    unit = "signal" if args.mode == "signals" else "message"
    print(f"Extracted {len(records)} {unit}(s) from {args.chat} -> {out_path}")


if __name__ == "__main__":
    main()
