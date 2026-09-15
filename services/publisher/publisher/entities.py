from __future__ import annotations

from typing import Any

from telethon.tl import types as tl_types

# Every MessageEntity* TL type, keyed by its class name. Telethon's `.to_dict()`
# (used by the listener to build `raw_entities`) always includes that class name
# under the "_" key with all of the type's own fields as plain kwargs, so this
# registry lets us reconstruct the exact TLObject Telethon expects for sending
# without hand-maintaining a per-type mapping.
_ENTITY_CLASSES: dict[str, type] = {
    name: cls
    for name, cls in vars(tl_types).items()
    if isinstance(cls, type) and name.startswith("MessageEntity")
}


def rebuild_entities(
    raw_entities: list[dict[str, Any]] | None, *, account_is_premium: bool = False
) -> list[Any]:
    """Rebuild Telethon MessageEntity objects from `raw_entities` dicts.

    Custom-emoji entities only render for Telegram Premium accounts; when the
    sending account isn't Premium, they're dropped and the fallback emoji text
    already present in raw_text is left in place (§6.6).
    """
    entities: list[Any] = []
    for raw in raw_entities or []:
        kind = raw.get("_")
        cls = _ENTITY_CLASSES.get(kind)
        if cls is None:
            continue
        if kind == "MessageEntityCustomEmoji" and not account_is_premium:
            continue
        kwargs = {k: v for k, v in raw.items() if k != "_"}
        try:
            entities.append(cls(**kwargs))
        except TypeError:
            continue
    return entities
