"""Slack Connect regression against the v0.23.0 adapter handler."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.slack.adapter import SlackAdapter


def _adapter(mentioned):
    adapter = SlackAdapter(PlatformConfig(
        enabled=True, token="fake", extra={"require_mention": mentioned}))
    adapter._app = MagicMock()
    adapter._app.client = AsyncMock()
    adapter._bot_user_id = "U_ELITE_BOT"
    adapter._team_bot_user_ids.update({
        "T_ELITE": "U_ELITE_BOT", "T_CLIENT": "U_CLIENT_BOT"})
    adapter._fetch_thread_context = AsyncMock(return_value=None)
    adapter._fetch_thread_parent_text = AsyncMock(return_value="")
    adapter.handle_message = AsyncMock()
    return adapter


def _message(team, bot_id, ts, parent, mentioned):
    return {
        "type": "message", "user": "U_HUMAN", "client_msg_id": "real-client-message",
        "channel": "C_SHARED", "channel_type": "channel", "team": team,
        "ts": ts, "thread_ts": parent,
        "text": f"<@{bot_id}> status?" if mentioned else "status?",
    }


@pytest.mark.parametrize("mentioned", [False, True])
@pytest.mark.asyncio
async def test_two_team_and_two_bot_id_copies_make_one_group_turn(mentioned):
    adapter = _adapter(mentioned)
    await asyncio.gather(
        adapter._handle_slack_message(_message(
            "T_ELITE", "U_ELITE_BOT", "174.001", "170.000", mentioned)),
        adapter._handle_slack_message(_message(
            "T_CLIENT", "U_CLIENT_BOT", "174.001", "170.000", mentioned)),
    )
    assert adapter.handle_message.await_count == 1
    event = adapter.handle_message.await_args.args[0]
    assert event.source.chat_type == "group"
    assert event.message_id == "174.001"

    # A new ask in the same thread has its own message ts and gets a turn.
    await adapter._handle_slack_message(_message(
        "T_CLIENT", "U_CLIENT_BOT", "174.002", "170.000", mentioned))
    assert adapter.handle_message.await_count == 2


@pytest.mark.asyncio
async def test_file_shared_fallback_uses_same_cross_team_key(monkeypatch):
    adapter = _adapter(False)
    adapter._handle_slack_message = AsyncMock()
    for team in ("T_ELITE", "T_CLIENT"):
        client = AsyncMock()
        client.files_info.return_value = {
            "ok": True,
            "file": {"id": "F_SHARED", "mimetype": "video/mp4", "user": "U_HUMAN",
                     "shares": {"public": {"C_SHARED": [
                         {"ts": "175.001", "thread_ts": "170.000"}]}}},
        }
        adapter._team_clients[team] = client
    monkeypatch.setattr("plugins.platforms.slack.adapter.asyncio.sleep", AsyncMock())

    await adapter._handle_slack_file_shared({
        "channel_id": "C_SHARED", "file_id": "F_SHARED", "team_id": "T_ELITE"})
    await adapter._handle_slack_file_shared({
        "channel_id": "C_SHARED", "file_id": "F_SHARED", "team_id": "T_CLIENT"})

    adapter._handle_slack_message.assert_awaited_once()
