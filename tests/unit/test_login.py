from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from signal_shared import login as login_module


class FakeDialog:
    def __init__(self, dialog_id: int, name: str, entity: object) -> None:
        self.id = dialog_id
        self.name = name
        self.entity = entity


class FakeDialogs:
    def __init__(self, dialogs: list[FakeDialog]) -> None:
        self._dialogs = dialogs

    def __call__(self, limit: int = 30):
        return self

    async def __aiter__(self):
        for dialog in self._dialogs:
            yield dialog


def make_fake_client(dialogs: list[FakeDialog]) -> Mock:
    client = Mock()
    client.start = AsyncMock()
    client.disconnect = AsyncMock()
    client.get_me = AsyncMock(return_value=Mock(username="operator", id=42))
    client.iter_dialogs = FakeDialogs(dialogs)
    return client


@pytest.mark.asyncio
async def test_run_interactive_login_chmods_session_and_lists_dialogs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session_path = tmp_path / "session" / "listener.session"

    async def fake_permissions(client, entity):
        from signal_shared.telegram import ChatPermission

        return ChatPermission(chat_type="group", can_send=True)

    dialogs = [FakeDialog(-1001, "My Group", object())]
    fake_client = make_fake_client(dialogs)

    monkeypatch.setattr(login_module, "make_client", lambda *a, **k: fake_client)
    monkeypatch.setattr(login_module, "describe_chat_permissions", fake_permissions)

    async def touch_session():
        session_path.write_text("session-bytes")

    fake_client.start.side_effect = touch_session

    await login_module.run_interactive_login(
        session_path=str(session_path), api_id=1, api_hash="hash", device_model="signal-listener"
    )

    assert session_path.exists()
    assert (session_path.stat().st_mode & 0o777) == 0o600

    fake_client.disconnect.assert_awaited_once()

    out = capsys.readouterr().out
    assert "Logged in as operator" in out
    assert "-1001\tgroup\tMy Group\t-\tyes" in out


@pytest.mark.asyncio
async def test_run_interactive_login_prints_username_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session_path = tmp_path / "session" / "publisher.session"

    async def fake_permissions(client, entity):
        from signal_shared.telegram import ChatPermission

        return ChatPermission(chat_type="bot", can_send=True)

    bot_entity = Mock()
    bot_entity.username = "HTBot_bot"
    dialogs = [FakeDialog(8181885491, "HTBot", bot_entity)]
    fake_client = make_fake_client(dialogs)

    monkeypatch.setattr(login_module, "make_client", lambda *a, **k: fake_client)
    monkeypatch.setattr(login_module, "describe_chat_permissions", fake_permissions)

    await login_module.run_interactive_login(
        session_path=str(session_path), api_id=1, api_hash="hash", device_model="signal-publisher"
    )

    out = capsys.readouterr().out
    assert "8181885491\tbot\tHTBot\t@HTBot_bot\tyes" in out


@pytest.mark.asyncio
async def test_run_interactive_login_handles_dialog_permission_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session_path = tmp_path / "session" / "publisher.session"

    async def failing_permissions(client, entity):
        raise RuntimeError("boom")

    dialogs = [FakeDialog(-2002, "Broken Dialog", object())]
    fake_client = make_fake_client(dialogs)

    monkeypatch.setattr(login_module, "make_client", lambda *a, **k: fake_client)
    monkeypatch.setattr(login_module, "describe_chat_permissions", failing_permissions)

    await login_module.run_interactive_login(
        session_path=str(session_path), api_id=1, api_hash="hash", device_model="signal-publisher"
    )

    out = capsys.readouterr().out
    assert "-2002\tunknown\tBroken Dialog\t-\tno" in out
