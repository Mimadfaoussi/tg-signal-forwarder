from __future__ import annotations

from typing import Any

from signal_shared.classifier import is_signal
from signal_shared.models import Signal
from signal_shared.parser import parse_signal


def extract_from_messages(
    messages: list[Any], *, chat_id: int, quote_assets: list[str]
) -> list[Signal]:
    """Pure classify+parse pass over a batch of Telethon-like messages (only
    `.raw_text`, `.id`, `.date`, `.entities` are used) -- the same per-message
    logic `listener.main.handle_message` applies live, factored out here so
    it's reusable for pulling a channel's whole history on demand (see
    `listener.extract`) and testable without a real Telegram connection.
    """
    signals: list[Signal] = []
    for message in messages:
        text = message.raw_text or ""
        if not is_signal(text, quote_assets=quote_assets):
            continue
        raw_entities = [e.to_dict() for e in (message.entities or [])]
        signal = parse_signal(
            text,
            signal_id=f"{chat_id}:{message.id}",
            source_chat_id=chat_id,
            source_message_id=message.id,
            received_at=message.date,
            raw_entities=raw_entities,
            quote_assets=quote_assets,
        )
        signals.append(signal)
    return signals
