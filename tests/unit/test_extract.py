from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from signal_shared.extract import extract_from_messages

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@dataclass
class FakeMessage:
    id: int
    raw_text: str
    date: datetime = field(default_factory=lambda: datetime(2026, 9, 14, 12, 0, tzinfo=UTC))
    entities: list | None = None
    sender_id: int | None = None


def test_only_classifiable_messages_become_signals() -> None:
    messages = [
        FakeMessage(id=1, raw_text=_read_fixture("signal_saga.txt")),
        FakeMessage(id=2, raw_text="good morning everyone!"),
        FakeMessage(id=3, raw_text=_read_fixture("signal_gps.txt")),
    ]

    signals = extract_from_messages(messages, chat_id=-1001, quote_assets=["USDT"])

    assert len(signals) == 2
    assert {s.source_message_id for s in signals} == {1, 3}


def test_signal_id_and_source_fields_are_set_from_the_message() -> None:
    messages = [FakeMessage(id=42, raw_text=_read_fixture("signal_saga.txt"))]

    [signal] = extract_from_messages(messages, chat_id=-1004463995445, quote_assets=["USDT"])

    assert signal.signal_id == "-1004463995445:42"
    assert signal.source_chat_id == -1004463995445
    assert signal.source_message_id == 42
    assert signal.pair == "SAGA/USDT"
    assert signal.parse_ok is True


def test_empty_history_yields_no_signals() -> None:
    assert extract_from_messages([], chat_id=-1001, quote_assets=["USDT"]) == []


def test_entities_are_serialized_via_to_dict() -> None:
    class FakeEntity:
        def to_dict(self) -> dict:
            return {"_": "MessageEntityBold", "offset": 0, "length": 4}

    messages = [
        FakeMessage(id=1, raw_text=_read_fixture("signal_saga.txt"), entities=[FakeEntity()])
    ]

    [signal] = extract_from_messages(messages, chat_id=-1001, quote_assets=["USDT"])

    assert signal.raw_entities == [{"_": "MessageEntityBold", "offset": 0, "length": 4}]
