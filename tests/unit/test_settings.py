import pytest
from pydantic import ValidationError
from signal_shared.settings import BaseAppSettings, describe_config_error, split_csv


class _RequiredFieldSettings(BaseAppSettings):
    secret_value: str
    other_required: str


def test_describe_config_error_names_missing_fields_without_leaking_values() -> None:
    with pytest.raises(ValidationError) as exc_info:
        _RequiredFieldSettings(
            _env_file=None,  # type: ignore[call-arg]
            secret_value="TOP-SECRET-DO-NOT-LEAK",
        )

    message = describe_config_error(exc_info.value)

    assert "other_required" in message
    assert "missing" in message
    assert "TOP-SECRET-DO-NOT-LEAK" not in message


def test_quote_assets_list_splits_and_normalizes() -> None:
    settings = BaseAppSettings(_env_file=None, quote_assets="usdt, btc ,, eth")  # type: ignore[call-arg]
    assert settings.quote_assets_list == ["USDT", "BTC", "ETH"]


def test_quote_assets_list_default() -> None:
    settings = BaseAppSettings(_env_file=None)  # type: ignore[call-arg]
    assert settings.quote_assets_list == ["USDT"]


def test_split_csv_trims_and_drops_empty_parts() -> None:
    assert split_csv("-100111, -100222 ,,@somechannel") == ["-100111", "-100222", "@somechannel"]


def test_split_csv_single_value() -> None:
    assert split_csv("-1004463995445") == ["-1004463995445"]


def test_split_csv_empty_string() -> None:
    assert split_csv("") == []
