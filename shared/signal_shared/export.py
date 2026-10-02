from __future__ import annotations

from typing import Any

from telethon.helpers import add_surrogate, del_surrogate

MEDIA_PLACEHOLDER = "(File not included. Change data exporting settings to download.)"

# Telethon MessageEntity* class name -> Telegram Desktop export entity "type".
# Entities with no entry here (and unrecognized future types) are emitted as
# "plain" -- the text survives, only the styling annotation is dropped.
_SIMPLE_ENTITY_TYPES = {
    "MessageEntityBold": "bold",
    "MessageEntityItalic": "italic",
    "MessageEntityUnderline": "underline",
    "MessageEntityStrike": "strikethrough",
    "MessageEntityCode": "code",
    "MessageEntityHashtag": "hashtag",
    "MessageEntityCashtag": "cashtag",
    "MessageEntityBotCommand": "bot_command",
    "MessageEntityMention": "mention",
    "MessageEntityEmail": "email",
    "MessageEntityPhone": "phone",
    "MessageEntityBankCard": "bank_card",
    "MessageEntitySpoiler": "spoiler",
    "MessageEntityBlockquote": "blockquote",
    "MessageEntityUrl": "link",
}

_PEER_KIND_BY_CLASS = {
    "PeerUser": ("user", "user_id"),
    "PeerChat": ("chat", "chat_id"),
    "PeerChannel": ("channel", "channel_id"),
}


def peer_id_string(peer: Any) -> str | None:
    """Telegram Desktop's `"user<id>"`/`"chat<id>"`/`"channel<id>"` id format."""
    if peer is None:
        return None
    kind = _PEER_KIND_BY_CLASS.get(type(peer).__name__)
    if kind is None:
        return None
    label, attr = kind
    return f"{label}{getattr(peer, attr)}"


def _peer_id_string_from_user_id(user_id: int) -> str:
    return f"user{user_id}"


def _display_name(entity: Any) -> str | None:
    if entity is None:
        return None
    first = getattr(entity, "first_name", None)
    last = getattr(entity, "last_name", None)
    if first or last:
        return " ".join(p for p in (first, last) if p)
    title = getattr(entity, "title", None)
    if title:
        return str(title)
    username = getattr(entity, "username", None)
    if username:
        return str(username)
    return None


def _entity_type_and_extra(entity: Any) -> tuple[str | None, dict[str, Any]]:
    name = type(entity).__name__
    if name == "MessageEntityPre":
        return "pre", {"language": getattr(entity, "language", "") or ""}
    if name == "MessageEntityTextUrl":
        return "text_link", {"href": entity.url}
    if name == "MessageEntityMentionName":
        return "mention_name", {"user_id": entity.user_id}
    if name == "MessageEntityCustomEmoji":
        return "custom_emoji", {"document_id": str(entity.document_id)}
    simple = _SIMPLE_ENTITY_TYPES.get(name)
    if simple is not None:
        return simple, {}
    return None, {}


def split_text_with_entities(
    raw_text: str, entities: list[Any]
) -> tuple[str | list[Any], list[dict[str, Any]]]:
    """Segment `raw_text` by `entities` into Telegram Desktop's two paired
    fields: `text` (a bare string when there's no formatting, otherwise a
    list mixing plain strings with entity dicts) and `text_entities` (always
    a list, with every segment -- including plain ones -- an explicit dict).

    Entity `offset`/`length` are UTF-16 code units, not Python code points,
    so slicing goes through `add_surrogate`/`del_surrogate` rather than
    indexing `raw_text` directly -- otherwise any character outside the
    Basic Multilingual Plane (many emoji) would shift every later offset.
    """
    if not raw_text:
        return "", []

    surrogate_text = add_surrogate(raw_text)
    sorted_entities = sorted(entities, key=lambda e: e.offset)

    text_pieces: list[Any] = []
    text_entities: list[dict[str, Any]] = []
    pos = 0
    for entity in sorted_entities:
        start, length = entity.offset, entity.length
        if start < pos:
            continue  # overlapping/malformed -- keep the earlier one, drop this
        if start > pos:
            plain = del_surrogate(surrogate_text[pos:start])
            text_pieces.append(plain)
            text_entities.append({"type": "plain", "text": plain})

        piece_text = del_surrogate(surrogate_text[start : start + length])
        type_name, extra = _entity_type_and_extra(entity)
        if type_name is None:
            text_pieces.append(piece_text)
            text_entities.append({"type": "plain", "text": piece_text})
        else:
            entry = {"type": type_name, "text": piece_text, **extra}
            text_pieces.append(entry)
            text_entities.append(entry)
        pos = start + length

    if pos < len(surrogate_text):
        plain = del_surrogate(surrogate_text[pos:])
        text_pieces.append(plain)
        text_entities.append({"type": "plain", "text": plain})

    if len(text_pieces) == 1 and isinstance(text_pieces[0], str):
        return text_pieces[0], text_entities
    return text_pieces, text_entities


def _rich_text_to_plain(value: Any) -> str:
    """Poll questions/answers are `TextWithEntities` in newer Telethon
    schema layers -- approximated here as plain text (no entity styling)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    text = getattr(value, "text", None)
    return str(text) if text is not None else str(value)


def _resolve_peer_and_name(peer: Any, cached_entity: Any) -> tuple[str | None, str | None]:
    peer_id = peer_id_string(peer)
    name = _display_name(cached_entity) or peer_id
    return name, peer_id


def _resolve_from(message: Any) -> tuple[str | None, str | None]:
    if message.from_id is not None:
        return _resolve_peer_and_name(message.from_id, getattr(message, "sender", None))
    # A channel post with no explicit sender: the channel itself is the sender.
    return _resolve_peer_and_name(message.peer_id, getattr(message, "chat", None))


def _largest_photo_size(sizes: list[Any]) -> Any | None:
    sized = [s for s in sizes if hasattr(s, "w") and hasattr(s, "h")]
    if not sized:
        return None
    return max(sized, key=lambda s: getattr(s, "size", None) or (s.w * s.h))


def _build_media_fields(message: Any) -> dict[str, Any]:
    media = getattr(message, "media", None)
    if media is None:
        return {}
    media_name = type(media).__name__

    if media_name == "MessageMediaPhoto":
        fields: dict[str, Any] = {"photo": MEDIA_PLACEHOLDER}
        largest = _largest_photo_size(getattr(media.photo, "sizes", None) or [])
        if largest is not None:
            fields["photo_file_size"] = getattr(largest, "size", None)
            fields["width"] = largest.w
            fields["height"] = largest.h
        return fields

    if media_name == "MessageMediaDocument":
        document = media.document
        if document is None:
            return {}
        attrs = {type(a).__name__: a for a in getattr(document, "attributes", [])}
        file_name = attrs.get("DocumentAttributeFilename")
        base: dict[str, Any] = {"mime_type": document.mime_type, "file_size": document.size}

        if "DocumentAttributeSticker" in attrs:
            sticker = attrs["DocumentAttributeSticker"]
            return {
                "file": MEDIA_PLACEHOLDER,
                "thumbnail": MEDIA_PLACEHOLDER,
                "media_type": "sticker",
                "sticker_emoji": sticker.alt,
                **({"file_name": file_name.file_name} if file_name else {}),
                **base,
            }

        if "DocumentAttributeVideo" in attrs:
            video = attrs["DocumentAttributeVideo"]
            return {
                "file": MEDIA_PLACEHOLDER,
                "thumbnail": MEDIA_PLACEHOLDER,
                "media_type": "round_video_message" if video.round_message else "video_file",
                "duration_seconds": video.duration,
                "width": video.w,
                "height": video.h,
                **base,
            }

        if "DocumentAttributeAudio" in attrs:
            audio = attrs["DocumentAttributeAudio"]
            if audio.voice:
                return {
                    "file": MEDIA_PLACEHOLDER,
                    "media_type": "voice_message",
                    "duration_seconds": audio.duration,
                    **base,
                }
            return {
                "file": MEDIA_PLACEHOLDER,
                "media_type": "audio_file",
                "duration_seconds": audio.duration,
                "performer": audio.performer,
                "title": audio.title,
                **base,
            }

        if "DocumentAttributeAnimated" in attrs:
            return {
                "file": MEDIA_PLACEHOLDER,
                "thumbnail": MEDIA_PLACEHOLDER,
                "media_type": "animation",
                **base,
            }

        return {
            "file": MEDIA_PLACEHOLDER,
            **({"file_name": file_name.file_name} if file_name else {}),
            **base,
        }

    return {}


def _build_reactions(reactions: Any) -> list[dict[str, Any]]:
    results = getattr(reactions, "results", None) if reactions is not None else None
    if not results:
        return []
    out: list[dict[str, Any]] = []
    for count in results:
        reaction = count.reaction
        reaction_name = type(reaction).__name__
        if reaction_name == "ReactionEmoji":
            out.append({"type": "emoji", "count": count.count, "emoji": reaction.emoticon})
        elif reaction_name == "ReactionCustomEmoji":
            out.append(
                {
                    "type": "custom_emoji",
                    "count": count.count,
                    "document_id": str(reaction.document_id),
                }
            )
        elif reaction_name == "ReactionPaid":
            out.append({"type": "paid", "count": count.count})
    return out


def _build_poll(message: Any) -> dict[str, Any]:
    media = getattr(message, "media", None)
    if media is None or type(media).__name__ != "MessageMediaPoll":
        return {}
    poll = media.poll
    poll_results = media.results

    votes_by_option: dict[bytes, Any] = {}
    total_voters = 0
    if poll_results is not None:
        total_voters = poll_results.total_voters or 0
        for answer_voters in poll_results.results or []:
            votes_by_option[bytes(answer_voters.option)] = answer_voters

    answers = []
    for answer in poll.answers:
        vote = votes_by_option.get(bytes(answer.option))
        answers.append(
            {
                "text": _rich_text_to_plain(answer.text),
                "voters": vote.voters if vote is not None else 0,
                "chosen": bool(vote.chosen) if vote is not None else False,
            }
        )

    return {
        "question": _rich_text_to_plain(poll.question),
        "closed": bool(poll.closed),
        "total_voters": total_voters,
        "answers": answers,
    }


# Telethon MessageAction* class name -> Telegram Desktop service action handling.
# Each entry maps to the TD `action` string plus the extra fields it carries;
# action types outside this list (gifts, giveaways, boosts, calls, etc.) are
# out of scope -- they're exceedingly unlikely in a trade-signal channel's
# history -- and fall back to a labeled passthrough so nothing is silently
# dropped.
def _build_service_action(action: Any, *, pinned_message_id: int | None) -> dict[str, Any]:
    name = type(action).__name__

    if name == "MessageActionChannelCreate":
        return {"action": "create_channel", "title": action.title}
    if name == "MessageActionChatCreate":
        return {"action": "create_group", "title": action.title}
    if name == "MessageActionChatEditTitle":
        return {"action": "edit_group_title", "title": action.title}
    if name == "MessageActionChatDeletePhoto":
        return {"action": "delete_group_photo"}
    if name == "MessageActionHistoryClear":
        return {"action": "clear_history"}
    if name == "MessageActionChatJoinedByLink":
        return {"action": "join_group_by_link"}
    if name == "MessageActionChatAddUser":
        return {
            "action": "invite_members",
            "members": [_peer_id_string_from_user_id(u) for u in action.users],
        }
    if name == "MessageActionChatDeleteUser":
        return {
            "action": "remove_members",
            "members": [_peer_id_string_from_user_id(action.user_id)],
        }
    if name == "MessageActionPinMessage":
        return {"action": "pin_message", "message_id": pinned_message_id}
    if name == "MessageActionChatEditPhoto":
        fields: dict[str, Any] = {"action": "edit_group_photo", "photo": MEDIA_PLACEHOLDER}
        largest = _largest_photo_size(getattr(action.photo, "sizes", None) or [])
        if largest is not None:
            fields["photo_file_size"] = getattr(largest, "size", None)
            fields["width"] = largest.w
            fields["height"] = largest.h
        return fields

    return {"action": f"unsupported_{name}"}


def build_message_record(message: Any) -> dict[str, Any]:
    """Build one Telegram-Desktop-export-shaped message record from a
    Telethon `Message`/`MessageService`. Best-effort: formatting/media/
    reactions/polls/common service actions are covered; exotic media and
    service-action types fall back to a labeled placeholder rather than
    raising, since a trade-signal channel's history won't realistically
    contain them.
    """
    is_service = type(message).__name__ == "MessageService"

    record: dict[str, Any] = {
        "id": message.id,
        "type": "service" if is_service else "message",
        "date": message.date.strftime("%Y-%m-%dT%H:%M:%S") if message.date else None,
        "date_unixtime": str(int(message.date.timestamp())) if message.date else None,
    }

    reply_to = getattr(message, "reply_to", None)
    reply_to_msg_id = getattr(reply_to, "reply_to_msg_id", None) if reply_to is not None else None

    if is_service:
        actor_name, actor_id = _resolve_from(message)
        if actor_name is not None:
            record["actor"] = actor_name
        if actor_id is not None:
            record["actor_id"] = actor_id
        record.update(_build_service_action(message.action, pinned_message_id=reply_to_msg_id))
        return record

    from_name, from_id = _resolve_from(message)
    if from_name is not None:
        record["from"] = from_name
    if from_id is not None:
        record["from_id"] = from_id

    post_author = getattr(message, "post_author", None)
    if post_author:
        record["author"] = post_author

    edit_date = getattr(message, "edit_date", None)
    if edit_date:
        record["edited"] = edit_date.strftime("%Y-%m-%dT%H:%M:%S")
        record["edited_unixtime"] = str(int(edit_date.timestamp()))

    if reply_to_msg_id is not None:
        record["reply_to_message_id"] = reply_to_msg_id
        reply_peer = getattr(reply_to, "reply_to_peer_id", None)
        if reply_peer is not None:
            record["reply_to_peer_id"] = peer_id_string(reply_peer)

    fwd_from = getattr(message, "fwd_from", None)
    if fwd_from is not None:
        if fwd_from.from_name:
            record["forwarded_from"] = fwd_from.from_name
        elif fwd_from.from_id is not None:
            label = peer_id_string(fwd_from.from_id)
            record["forwarded_from"] = label
            record["forwarded_from_id"] = label

    text, text_entities = split_text_with_entities(message.message or "", message.entities or [])
    record["text"] = text
    record["text_entities"] = text_entities

    record.update(_build_media_fields(message))

    reactions = _build_reactions(getattr(message, "reactions", None))
    if reactions:
        record["reactions"] = reactions

    poll = _build_poll(message)
    if poll:
        record["poll"] = poll

    return record


def _chat_type_name(entity: Any) -> str:
    class_name = type(entity).__name__
    if class_name == "Channel":
        is_public = bool(getattr(entity, "username", None))
        if getattr(entity, "megagroup", False):
            return "public_supergroup" if is_public else "private_supergroup"
        return "public_channel" if is_public else "private_channel"
    if class_name == "Chat":
        return "private_group"
    if class_name == "User":
        return "personal_chat"
    return "private_channel"


def build_chat_export(entity: Any, messages: list[Any]) -> dict[str, Any]:
    """Build the Telegram-Desktop-export-shaped top-level document
    (`{name, type, id, messages}`) for a whole chat's history."""
    records = [build_message_record(m) for m in messages]
    records.sort(key=lambda r: r["date"] or "")
    return {
        "name": _display_name(entity) or str(entity.id),
        "type": _chat_type_name(entity),
        "id": entity.id,
        "messages": records,
    }
