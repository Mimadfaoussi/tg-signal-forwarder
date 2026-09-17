from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from signal_shared.parser import parse_signal

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _parse(text: str):
    return parse_signal(
        text,
        signal_id="-100123:1",
        source_chat_id=-100123,
        source_message_id=1,
        received_at=datetime(2026, 9, 14, 12, 0, tzinfo=UTC),
    )


def test_signal_saga() -> None:
    signal = _parse(_read("signal_saga.txt"))

    assert signal.parse_ok is True
    assert signal.pair == "SAGA/USDT"
    assert signal.base == "SAGA"
    assert signal.quote == "USDT"
    assert signal.entries == [Decimal("0.01807")]
    assert [tp.index for tp in signal.take_profits] == [1, 2, 3]
    assert signal.take_profits[0].price == Decimal("0.01850")
    assert signal.take_profits[0].percent == Decimal("2.38")
    assert signal.take_profits[1].price == Decimal("0.01890")
    assert signal.take_profits[1].percent == Decimal("4.59")
    assert signal.take_profits[2].price == Decimal("0.01954")
    assert signal.take_profits[2].percent == Decimal("8.13")
    assert signal.stop == Decimal("0.01794")
    assert signal.stop_note is None
    assert signal.signal_date == date(2026, 9, 14)


def test_signal_dash() -> None:
    signal = _parse(_read("signal_dash.txt"))

    assert signal.parse_ok is True
    assert signal.pair == "DASH/USDT"
    assert signal.entries == [Decimal("55.36")]
    assert len(signal.take_profits) == 5
    assert [tp.index for tp in signal.take_profits] == [1, 2, 3, 4, 5]
    assert signal.take_profits[4].price == Decimal("71.47")
    assert signal.take_profits[4].percent == Decimal("29.10")
    assert signal.stop == Decimal("52.89")
    assert signal.stop_note is None
    assert signal.signal_date == date(2026, 9, 14)


def test_signal_gps() -> None:
    signal = _parse(_read("signal_gps.txt"))

    assert signal.parse_ok is True
    assert signal.pair == "GPS/USDT"
    assert len(signal.take_profits) == 4
    assert signal.stop == Decimal("0.01043")
    assert signal.stop_note == "5m"
    assert signal.signal_date == date(2026, 9, 14)


def test_signal_arb_alternate_format() -> None:
    # "PAIR:" instead of "#", "T1"/"T2" instead of "TP1"/"TP2", "SL" instead
    # of "Stop", and a second parenthesised group after SL that must NOT
    # leak into stop_note.
    signal = _parse(_read("signal_arb.txt"))

    assert signal.parse_ok is True
    assert signal.pair == "ARB/USDT"
    assert signal.base == "ARB"
    assert signal.quote == "USDT"
    assert signal.entries == [Decimal("0.134"), Decimal("0.13026")]
    assert len(signal.take_profits) == 6
    assert [tp.index for tp in signal.take_profits] == [1, 2, 3, 4, 5, 6]
    assert signal.take_profits[0].price == Decimal("0.14042")
    assert signal.take_profits[0].percent == Decimal("4.79")
    assert signal.take_profits[5].price == Decimal("0.1749")
    assert signal.take_profits[5].percent == Decimal("30.52")
    assert signal.stop == Decimal("0.12685")
    assert signal.stop_note == "4h"
    assert signal.signal_date == date(2026, 9, 15)


def test_crlf_line_endings() -> None:
    text = "#SAGA/USDT\r\nEntry1: 0.01807\r\nTP1: 0.01850 (2.38%)\r\nStop: 0.01794\r\n"
    signal = _parse(text)

    assert signal.parse_ok is True
    assert signal.pair == "SAGA/USDT"
    assert signal.entries == [Decimal("0.01807")]
    assert signal.stop == Decimal("0.01794")


def test_no_emoji_plain_text() -> None:
    text = "#SAGA/USDT\nEntry1: 0.01807\nTP1: 0.01850\nStop: 0.01794\n"
    signal = _parse(text)

    assert signal.parse_ok is True
    assert signal.pair == "SAGA/USDT"


def test_lowercase_labels() -> None:
    text = "#saga/usdt\nentry1: 0.01807\ntp1: 0.01850\nstop: 0.01794\n"
    signal = _parse(text)

    assert signal.parse_ok is True
    assert signal.pair == "SAGA/USDT"
    assert signal.entries == [Decimal("0.01807")]
    assert signal.stop == Decimal("0.01794")


def test_entry_with_space_before_colon() -> None:
    text = "#SAGA/USDT\nEntry 1: 0.01807\nTP1: 0.01850\nStop: 0.01794\n"
    signal = _parse(text)

    assert signal.entries == [Decimal("0.01807")]


def test_two_entries() -> None:
    text = "#SAGA/USDT\nEntry1: 0.01807\nEntry2: 0.01700\nTP1: 0.01850\nStop: 0.01794\n"
    signal = _parse(text)

    assert signal.entries == [Decimal("0.01807"), Decimal("0.01700")]


def test_partial_parse_sets_parse_ok_false() -> None:
    text = "#SAGA/USDT only pair, nothing else"
    signal = _parse(text)

    assert signal.parse_ok is False
    assert signal.pair == "SAGA/USDT"
    assert signal.entries == []
    assert signal.stop is None
