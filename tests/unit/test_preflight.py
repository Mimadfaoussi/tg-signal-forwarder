from unittest.mock import AsyncMock, Mock

import pytest
from publisher.sender import TargetNotWritable, preflight
from signal_shared.telegram import describe_chat_permissions, make_client
from telethon import TelegramClient
from telethon.tl.custom.participantpermissions import ParticipantPermissions
from telethon.tl.types import (
    Channel,
    ChannelParticipantAdmin,
    ChannelParticipantBanned,
    Chat,
    ChatBannedRights,
    User,
)
from telethon.tl.types import (
    ChannelParticipant as ChannelParticipantPlain,
)


def make_channel(
    *, megagroup=False, forum=False, creator=False, admin_rights=None, left=False, slowmode=None
):
    channel = Mock(spec=Channel)
    channel.megagroup = megagroup
    channel.forum = forum
    channel.creator = creator
    channel.admin_rights = admin_rights
    channel.left = left
    channel.slowmode_seconds = slowmode
    channel.default_banned_rights = None
    return channel


def make_admin_rights(post_messages=True):
    rights = Mock()
    rights.post_messages = post_messages
    return rights


def make_banned_rights(send_messages=True):
    rights = Mock(spec=ChatBannedRights)
    rights.send_messages = send_messages
    return rights


class FakeLog:
    def info(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


@pytest.mark.asyncio
async def test_broadcast_channel_without_admin_rights_is_not_writable() -> None:
    channel = make_channel(megagroup=False, creator=False, admin_rights=None)
    permission = await describe_chat_permissions(client=Mock(), entity=channel)

    assert permission.can_send is False
    assert permission.chat_type == "channel"
    assert permission.reason == "broadcast_channel_requires_admin"


@pytest.mark.asyncio
async def test_broadcast_channel_with_post_rights_is_writable() -> None:
    channel = make_channel(
        megagroup=False, creator=False, admin_rights=make_admin_rights(post_messages=True)
    )
    permission = await describe_chat_permissions(client=Mock(), entity=channel)

    assert permission.can_send is True
    assert permission.chat_type == "channel"


@pytest.mark.asyncio
async def test_writable_supergroup_passes() -> None:
    channel = make_channel(megagroup=True, left=False, slowmode=20)
    participant = Mock(spec=ChannelParticipantPlain)
    perms = ParticipantPermissions(participant=participant, chat=False)

    client = Mock()
    client.get_permissions = AsyncMock(return_value=perms)

    permission = await describe_chat_permissions(client, channel)

    assert permission.can_send is True
    assert permission.chat_type == "supergroup"
    assert permission.slow_mode_delay == 20


@pytest.mark.asyncio
async def test_banned_supergroup_participant_is_not_writable() -> None:
    channel = make_channel(megagroup=True, left=False)
    participant = Mock(spec=ChannelParticipantBanned)
    participant.banned_rights = make_banned_rights(send_messages=True)
    perms = ParticipantPermissions(participant=participant, chat=False)

    client = Mock()
    client.get_permissions = AsyncMock(return_value=perms)

    permission = await describe_chat_permissions(client, channel)

    assert permission.can_send is False
    assert permission.reason == "restricted_from_sending"


@pytest.mark.asyncio
async def test_supergroup_with_default_send_restricted_is_not_writable() -> None:
    channel = make_channel(megagroup=True, left=False)
    channel.default_banned_rights = make_banned_rights(send_messages=True)
    participant = Mock(spec=ChannelParticipantPlain)
    perms = ParticipantPermissions(participant=participant, chat=False)

    client = Mock()
    client.get_permissions = AsyncMock(return_value=perms)

    permission = await describe_chat_permissions(client, channel)

    assert permission.can_send is False
    assert permission.reason == "restricted_from_sending"


@pytest.mark.asyncio
async def test_admin_in_supergroup_can_always_send() -> None:
    channel = make_channel(megagroup=True, left=False)
    channel.default_banned_rights = make_banned_rights(send_messages=True)
    participant = Mock(spec=ChannelParticipantAdmin)
    participant.admin_rights = make_admin_rights()
    perms = ParticipantPermissions(participant=participant, chat=False)

    client = Mock()
    client.get_permissions = AsyncMock(return_value=perms)

    permission = await describe_chat_permissions(client, channel)

    assert permission.can_send is True


@pytest.mark.asyncio
async def test_not_a_member_of_supergroup_is_not_writable() -> None:
    channel = make_channel(megagroup=True, left=True)
    permission = await describe_chat_permissions(client=Mock(), entity=channel)

    assert permission.can_send is False
    assert permission.reason == "not_a_member"


@pytest.mark.asyncio
async def test_bot_user_not_blocked_is_a_valid_target() -> None:
    bot_user = Mock(spec=User)
    bot_user.bot = True

    full_response = Mock()
    full_response.full_user = Mock()
    full_response.full_user.blocked = False

    client = AsyncMock(return_value=full_response)

    permission = await describe_chat_permissions(client, bot_user)

    assert permission.can_send is True
    assert permission.chat_type == "bot"


@pytest.mark.asyncio
async def test_bot_user_blocked_by_account_is_not_writable() -> None:
    bot_user = Mock(spec=User)
    bot_user.bot = True

    full_response = Mock()
    full_response.full_user = Mock()
    full_response.full_user.blocked = True

    client = AsyncMock(return_value=full_response)

    permission = await describe_chat_permissions(client, bot_user)

    assert permission.can_send is False
    assert permission.reason == "blocked_by_account"


@pytest.mark.asyncio
async def test_preflight_raises_target_not_writable() -> None:
    channel = make_channel(megagroup=False, creator=False, admin_rights=None)
    client = Mock()
    client.get_entity = AsyncMock(return_value=channel)

    with pytest.raises(TargetNotWritable):
        await preflight(client, "@somechannel", FakeLog())


@pytest.mark.asyncio
async def test_preflight_passes_for_writable_target() -> None:
    channel = make_channel(megagroup=False, creator=True, admin_rights=None)
    client = Mock()
    client.get_entity = AsyncMock(return_value=channel)

    permission = await preflight(client, "@somechannel", FakeLog())

    assert permission.can_send is True


def test_make_client_returns_a_telegram_client(tmp_path) -> None:
    client = make_client(str(tmp_path / "x.session"), 1, "hash", "device-model")
    try:
        assert isinstance(client, TelegramClient)
    finally:
        client.session.close()


@pytest.mark.asyncio
async def test_user_resolution_failure_is_unresolvable() -> None:
    user = Mock(spec=User)
    user.bot = False

    client = AsyncMock(side_effect=RuntimeError("network down"))

    permission = await describe_chat_permissions(client, user)

    assert permission.can_send is False
    assert permission.chat_type == "user"
    assert "unresolvable" in permission.reason


@pytest.mark.asyncio
async def test_writable_basic_group() -> None:
    group = Mock(spec=Chat)
    group.left = False
    group.deactivated = False
    group.default_banned_rights = None

    permission = await describe_chat_permissions(client=Mock(), entity=group)

    assert permission.can_send is True
    assert permission.chat_type == "group"


@pytest.mark.asyncio
async def test_basic_group_not_a_member() -> None:
    group = Mock(spec=Chat)
    group.left = True
    group.deactivated = False

    permission = await describe_chat_permissions(client=Mock(), entity=group)

    assert permission.can_send is False
    assert permission.reason == "not_a_member"


@pytest.mark.asyncio
async def test_basic_group_restricted_from_sending() -> None:
    group = Mock(spec=Chat)
    group.left = False
    group.deactivated = False
    group.default_banned_rights = make_banned_rights(send_messages=True)

    permission = await describe_chat_permissions(client=Mock(), entity=group)

    assert permission.can_send is False
    assert permission.reason == "restricted_from_sending"


@pytest.mark.asyncio
async def test_get_permissions_failure_is_unresolvable() -> None:
    channel = make_channel(megagroup=True, left=False)
    client = Mock()
    client.get_permissions = AsyncMock(side_effect=RuntimeError("boom"))

    permission = await describe_chat_permissions(client, channel)

    assert permission.can_send is False
    assert "unresolvable" in permission.reason


@pytest.mark.asyncio
async def test_get_permissions_returning_none_is_unresolvable() -> None:
    channel = make_channel(megagroup=True, left=False)
    client = Mock()
    client.get_permissions = AsyncMock(return_value=None)

    permission = await describe_chat_permissions(client, channel)

    assert permission.can_send is False
    assert permission.reason == "unresolvable"


@pytest.mark.asyncio
async def test_permissions_has_left_is_not_a_member() -> None:
    channel = make_channel(megagroup=True, left=False)
    participant = Mock(spec=ChannelParticipantPlain)
    perms = ParticipantPermissions(participant=participant, chat=False)
    perms_mock = Mock(wraps=perms)
    perms_mock.has_left = True
    perms_mock.is_banned = False

    client = Mock()
    client.get_permissions = AsyncMock(return_value=perms_mock)

    permission = await describe_chat_permissions(client, channel)

    assert permission.can_send is False
    assert permission.reason == "not_a_member"


@pytest.mark.asyncio
async def test_banned_participant_with_send_rights_still_intact_can_send() -> None:
    # Edge case: participant is a ChannelParticipantBanned record (e.g. some
    # other right was revoked) but send_messages specifically was not.
    channel = make_channel(megagroup=True, left=False, slowmode=5)
    participant = Mock(spec=ChannelParticipantBanned)
    participant.banned_rights = make_banned_rights(send_messages=False)
    perms = ParticipantPermissions(participant=participant, chat=False)

    client = Mock()
    client.get_permissions = AsyncMock(return_value=perms)

    permission = await describe_chat_permissions(client, channel)

    assert permission.can_send is True
    assert permission.slow_mode_delay == 5


@pytest.mark.asyncio
async def test_unknown_entity_type_is_unresolvable() -> None:
    permission = await describe_chat_permissions(client=Mock(), entity=object())

    assert permission.can_send is False
    assert permission.chat_type == "unknown"
    assert permission.reason == "unresolvable"
