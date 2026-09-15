from pathlib import Path

import pytest
from signal_shared.classifier import is_signal

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "fixture",
    ["signal_saga.txt", "signal_dash.txt", "signal_gps.txt"],
)
def test_signal_fixtures_are_signals(fixture: str) -> None:
    assert is_signal(_read(fixture)) is True


@pytest.mark.parametrize(
    "fixture",
    ["update_entry_hit.txt", "update_tp_hit.txt"],
)
def test_update_fixtures_are_not_signals(fixture: str) -> None:
    assert is_signal(_read(fixture)) is False


def test_empty_text_is_not_a_signal() -> None:
    assert is_signal("") is False


def test_none_text_is_not_a_signal() -> None:
    assert is_signal(None) is False
