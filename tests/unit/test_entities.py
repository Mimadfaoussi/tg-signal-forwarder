from publisher.entities import rebuild_entities
from telethon.tl import types as tl_types


def test_bold_round_trip() -> None:
    original = tl_types.MessageEntityBold(offset=0, length=4)
    rebuilt = rebuild_entities([original.to_dict()])
    assert len(rebuilt) == 1
    assert isinstance(rebuilt[0], tl_types.MessageEntityBold)
    assert rebuilt[0].offset == 0
    assert rebuilt[0].length == 4


def test_italic_round_trip() -> None:
    original = tl_types.MessageEntityItalic(offset=5, length=6)
    rebuilt = rebuild_entities([original.to_dict()])
    assert len(rebuilt) == 1
    assert isinstance(rebuilt[0], tl_types.MessageEntityItalic)
    assert rebuilt[0].offset == 5
    assert rebuilt[0].length == 6


def test_text_url_round_trip() -> None:
    original = tl_types.MessageEntityTextUrl(offset=3, length=7, url="https://example.com")
    rebuilt = rebuild_entities([original.to_dict()])
    assert len(rebuilt) == 1
    assert isinstance(rebuilt[0], tl_types.MessageEntityTextUrl)
    assert rebuilt[0].offset == 3
    assert rebuilt[0].length == 7
    assert rebuilt[0].url == "https://example.com"


def test_custom_emoji_round_trip_when_premium() -> None:
    original = tl_types.MessageEntityCustomEmoji(
        offset=0, length=2, document_id=5432101234567890123
    )
    rebuilt = rebuild_entities([original.to_dict()], account_is_premium=True)
    assert len(rebuilt) == 1
    assert isinstance(rebuilt[0], tl_types.MessageEntityCustomEmoji)
    assert rebuilt[0].document_id == 5432101234567890123


def test_custom_emoji_dropped_when_not_premium() -> None:
    original = tl_types.MessageEntityCustomEmoji(offset=0, length=2, document_id=123)
    rebuilt = rebuild_entities([original.to_dict()], account_is_premium=False)
    assert rebuilt == []


def test_offsets_stay_utf16_through_round_trip() -> None:
    # "😀" is a surrogate pair (2 UTF-16 code units); Telethon's own offsets are
    # already in UTF-16 units, so they must survive the dict round-trip unchanged.
    utf16_offset = len("😀".encode("utf-16-le")) // 2  # == 2
    original = tl_types.MessageEntityBold(offset=utf16_offset, length=4)
    rebuilt = rebuild_entities([original.to_dict()])
    assert rebuilt[0].offset == 2
    assert rebuilt[0].length == 4


def test_none_and_empty_input() -> None:
    assert rebuild_entities(None) == []
    assert rebuild_entities([]) == []


def test_unknown_entity_type_is_dropped() -> None:
    result = rebuild_entities([{"_": "MessageEntitySomethingNew", "offset": 0, "length": 1}])
    assert result == []


def test_multiple_entities_preserve_order() -> None:
    raw = [
        tl_types.MessageEntityBold(offset=0, length=4).to_dict(),
        tl_types.MessageEntityItalic(offset=5, length=6).to_dict(),
    ]
    rebuilt = rebuild_entities(raw)
    assert [type(e) for e in rebuilt] == [tl_types.MessageEntityBold, tl_types.MessageEntityItalic]
