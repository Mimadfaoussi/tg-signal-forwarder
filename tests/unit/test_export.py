from datetime import UTC, datetime

from signal_shared.export import (
    MEDIA_PLACEHOLDER,
    build_chat_export,
    build_message_record,
    split_text_with_entities,
)
from telethon.tl import types as t

SOME_DATE = datetime(2026, 9, 14, 12, 30, tzinfo=UTC)


def _message(**kwargs: object) -> t.Message:
    defaults: dict[str, object] = {
        "id": 1,
        "peer_id": t.PeerChannel(channel_id=999),
        "date": SOME_DATE,
        "message": "",
    }
    defaults.update(kwargs)
    return t.Message(**defaults)  # type: ignore[arg-type]


def _service(**kwargs: object) -> t.MessageService:
    defaults: dict[str, object] = {
        "id": 1,
        "peer_id": t.PeerChannel(channel_id=999),
        "date": SOME_DATE,
        "action": t.MessageActionChannelCreate(title="x"),
    }
    defaults.update(kwargs)
    return t.MessageService(**defaults)  # type: ignore[arg-type]


# -- split_text_with_entities --------------------------------------------


def test_no_entities_collapses_to_a_bare_string() -> None:
    text, text_entities = split_text_with_entities("hello world", [])

    assert text == "hello world"
    assert text_entities == [{"type": "plain", "text": "hello world"}]


def test_empty_text_yields_empty_results() -> None:
    assert split_text_with_entities("", []) == ("", [])


def test_bold_entity_splits_into_plain_and_entity_pieces() -> None:
    entity = t.MessageEntityBold(offset=6, length=5)

    text, text_entities = split_text_with_entities("hello world", [entity])

    assert text == ["hello ", {"type": "bold", "text": "world"}]
    assert text_entities == [
        {"type": "plain", "text": "hello "},
        {"type": "bold", "text": "world"},
    ]


def test_entity_offsets_are_utf16_code_units_not_code_points() -> None:
    # "😀" is one Python code point but two UTF-16 code units, so an entity
    # placed after it must use offset=2, not offset=1, to land correctly.
    entity = t.MessageEntityBold(offset=2, length=4)

    text, _ = split_text_with_entities("😀bold", [entity])

    assert text == ["😀", {"type": "bold", "text": "bold"}]


def test_text_url_entity_carries_href() -> None:
    entity = t.MessageEntityTextUrl(offset=0, length=4, url="https://example.com")

    _, text_entities = split_text_with_entities("link", [entity])

    assert text_entities == [{"type": "text_link", "text": "link", "href": "https://example.com"}]


def test_unrecognized_entity_type_falls_back_to_plain() -> None:
    entity = t.MessageEntityUnknown(offset=0, length=5)

    text, text_entities = split_text_with_entities("hello", [entity])

    assert text == "hello"
    assert text_entities == [{"type": "plain", "text": "hello"}]


# -- build_message_record: regular messages ------------------------------


def test_channel_post_uses_the_channel_as_sender() -> None:
    message = _message(message="BTC/USDT LONG", from_id=None)
    message._chat = t.Channel(  # type: ignore[attr-defined]
        id=999, title="AL-MAHWASHI CRYPTO", photo=t.ChatPhotoEmpty(), date=None
    )

    record = build_message_record(message)

    assert record["type"] == "message"
    assert record["from"] == "AL-MAHWASHI CRYPTO"
    assert record["from_id"] == "channel999"
    assert record["text"] == "BTC/USDT LONG"
    assert record["date"] == "2026-09-14T12:30:00"
    assert record["date_unixtime"] == str(int(SOME_DATE.timestamp()))


def test_reply_to_message_fields() -> None:
    message = _message(
        reply_to=t.MessageReplyHeader(
            reply_to_msg_id=42, reply_to_peer_id=t.PeerChannel(channel_id=999)
        )
    )

    record = build_message_record(message)

    assert record["reply_to_message_id"] == 42
    assert record["reply_to_peer_id"] == "channel999"


def test_forwarded_from_uses_from_name_when_present() -> None:
    message = _message(fwd_from=t.MessageFwdHeader(date=None, from_name="Some Guy"))

    record = build_message_record(message)

    assert record["forwarded_from"] == "Some Guy"
    assert "forwarded_from_id" not in record


def test_reactions_are_mapped_from_reaction_count() -> None:
    message = _message(
        reactions=t.MessageReactions(
            results=[t.ReactionCount(reaction=t.ReactionEmoji(emoticon="❤"), count=3)]
        )
    )

    record = build_message_record(message)

    assert record["reactions"] == [{"type": "emoji", "count": 3, "emoji": "❤"}]


def test_poll_maps_question_and_answer_votes() -> None:
    poll = t.Poll(
        id=1,
        question="Which pair?",
        answers=[t.PollAnswer(text="BTC", option=b"0"), t.PollAnswer(text="ETH", option=b"1")],
        hash=0,
        closed=True,
    )
    results = t.PollResults(
        total_voters=8,
        results=[t.PollAnswerVoters(option=b"0", voters=5, chosen=True, correct=False)],
    )
    message = _message(media=t.MessageMediaPoll(poll=poll, results=results))

    record = build_message_record(message)

    assert record["poll"] == {
        "question": "Which pair?",
        "closed": True,
        "total_voters": 8,
        "answers": [
            {"text": "BTC", "voters": 5, "chosen": True},
            {"text": "ETH", "voters": 0, "chosen": False},
        ],
    }


def test_sticker_media_uses_the_placeholder_and_sticker_fields() -> None:
    document = t.Document(
        id=1,
        access_hash=1,
        file_reference=b"",
        date=None,
        mime_type="image/webp",
        size=123,
        dc_id=1,
        attributes=[
            t.DocumentAttributeSticker(alt="grinning", stickerset=t.InputStickerSetEmpty())
        ],
    )
    message = _message(media=t.MessageMediaDocument(document=document))

    record = build_message_record(message)

    assert record["file"] == MEDIA_PLACEHOLDER
    assert record["media_type"] == "sticker"
    assert record["sticker_emoji"] == "grinning"
    assert record["mime_type"] == "image/webp"
    assert record["file_size"] == 123


def test_edited_message_includes_edited_timestamp() -> None:
    message = _message(edit_date=SOME_DATE)

    record = build_message_record(message)

    assert record["edited"] == "2026-09-14T12:30:00"
    assert record["edited_unixtime"] == str(int(SOME_DATE.timestamp()))


# -- build_message_record: service messages ------------------------------


def test_pin_message_action_uses_the_reply_header_for_the_pinned_id() -> None:
    service = _service(
        action=t.MessageActionPinMessage(),
        reply_to=t.MessageReplyHeader(reply_to_msg_id=77),
        from_id=None,
    )
    service._chat = t.Channel(  # type: ignore[attr-defined]
        id=999, title="AL-MAHWASHI CRYPTO", photo=t.ChatPhotoEmpty(), date=None
    )

    record = build_message_record(service)

    assert record["type"] == "service"
    assert record["action"] == "pin_message"
    assert record["message_id"] == 77
    assert record["actor"] == "AL-MAHWASHI CRYPTO"


def test_create_channel_action_includes_title() -> None:
    service = _service(action=t.MessageActionChannelCreate(title="AL-MAHWASHI CRYPTO"))

    record = build_message_record(service)

    assert record == {
        "id": 1,
        "type": "service",
        "date": "2026-09-14T12:30:00",
        "date_unixtime": str(int(SOME_DATE.timestamp())),
        "actor": "channel999",
        "actor_id": "channel999",
        "action": "create_channel",
        "title": "AL-MAHWASHI CRYPTO",
    }


def test_unsupported_action_falls_back_to_a_labeled_passthrough() -> None:
    service = _service(action=t.MessageActionGameScore(game_id=1, score=1))

    record = build_message_record(service)

    assert record["action"] == "unsupported_MessageActionGameScore"


# -- build_chat_export -----------------------------------------------------


def test_build_chat_export_structure_and_sorting() -> None:
    entity = t.Channel(
        id=999, title="AL-MAHWASHI CRYPTO", photo=t.ChatPhotoEmpty(), date=None, broadcast=True
    )
    earlier = _message(id=1, message="first", date=datetime(2026, 9, 1, tzinfo=UTC))
    later = _message(id=2, message="second", date=datetime(2026, 9, 2, tzinfo=UTC))

    export = build_chat_export(entity, [later, earlier])

    assert export["name"] == "AL-MAHWASHI CRYPTO"
    assert export["type"] == "private_channel"
    assert export["id"] == 999
    assert [m["id"] for m in export["messages"]] == [1, 2]


def test_build_chat_export_public_channel_type() -> None:
    entity = t.Channel(
        id=999,
        title="AL-MAHWASHI CRYPTO",
        photo=t.ChatPhotoEmpty(),
        date=None,
        broadcast=True,
        username="al_mahwashi",
    )

    export = build_chat_export(entity, [])

    assert export["type"] == "public_channel"
    assert export["messages"] == []
