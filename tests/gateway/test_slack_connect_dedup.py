"""Slack Connect delivers one channel message through each member workspace."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.slack.adapter import SlackAdapter


def _message(team, channel, ts, parent):
    return {
        "type": "message", "text": "<@U_BOT> status?", "user": "U_CLIENT",
        "client_msg_id": "client-message", "channel": channel,
        "channel_type": "channel", "team": team, "ts": ts,
        "thread_ts": parent,
    }


def _adapter():
    adapter = SlackAdapter(PlatformConfig(enabled=True, token="fake"))
    adapter._app = MagicMock()
    adapter._app.client = AsyncMock()
    adapter._bot_user_id = "U_BOT"
    adapter._team_bot_user_ids.update({"T_ELITE": "U_BOT", "T_CLIENT": "U_BOT"})
    adapter.handle_message = AsyncMock()
    adapter._fetch_thread_context = AsyncMock(return_value=None)
    adapter._fetch_thread_parent_text = AsyncMock(return_value="")
    return adapter


@pytest.mark.asyncio
async def test_slack_connect_duplicate_runs_once_but_distinct_messages_survive():
    adapter = _adapter()

    await asyncio.gather(
        adapter._handle_slack_message(_message("T_ELITE", "C_SHARED", "171.001", "170.000")),
        adapter._handle_slack_message(_message("T_CLIENT", "C_SHARED", "171.001", "170.000")),
    )
    assert adapter.handle_message.await_count == 1

    await adapter._handle_slack_message(_message("T_CLIENT", "C_SHARED", "171.002", "170.000"))
    await adapter._handle_slack_message(_message("T_CLIENT", "C_OTHER", "171.001", "170.000"))
    await adapter._handle_slack_message(_message("T_CLIENT", "C_SHARED", "171.001", "169.000"))
    assert adapter.handle_message.await_count == 4


@pytest.mark.asyncio
async def test_edit_can_add_mention_after_original_was_ignored():
    adapter = _adapter()
    original = _message("T_ELITE", "C_SHARED", "172.001", "170.000")
    original["text"] = "status?"
    await adapter._handle_slack_message(original)
    assert adapter.handle_message.await_count == 0

    await adapter._handle_slack_message({
        "type": "message", "subtype": "message_changed", "channel": "C_SHARED",
        "channel_type": "channel", "team": "T_CLIENT", "event_ts": "172.002",
        "message": {**original, "text": "<@U_BOT> status?", "edited": {"ts": "172.002"}},
    })
    assert adapter.handle_message.await_count == 1


@pytest.mark.asyncio
async def test_same_dm_ids_in_different_workspaces_still_run_twice():
    adapter = _adapter()
    event = _message("T_ELITE", "D_SHARED", "173.001", "")
    event["channel_type"] = "im"
    await adapter._handle_slack_message(event)
    await adapter._handle_slack_message({**event, "team": "T_CLIENT"})
    assert adapter.handle_message.await_count == 2


@pytest.mark.parametrize("mentioned", [False, True])
@pytest.mark.asyncio
async def test_shared_message_with_distinct_workspace_bot_ids_runs_once(mentioned):
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

    first = _message("T_ELITE", "C_SHARED", "174.001", "170.000")
    second = _message("T_CLIENT", "C_SHARED", "174.001", "170.000")
    first["text"] = "<@U_ELITE_BOT> status?" if mentioned else "status?"
    second["text"] = "<@U_CLIENT_BOT> status?" if mentioned else "status?"
    await asyncio.gather(
        adapter._handle_slack_message(first),
        adapter._handle_slack_message(second),
    )

    assert adapter.handle_message.await_count == 1


@pytest.mark.parametrize("first_team", ["T_CLIENT", "T_ELITE"])
@pytest.mark.asyncio
async def test_one_workspace_mention_survives_both_orders(first_team):
    adapter = SlackAdapter(PlatformConfig(
        enabled=True, token="fake", extra={"require_mention": True}))
    adapter._app = MagicMock()
    adapter._app.client = AsyncMock()
    adapter._bot_user_id = "U_ELITE_BOT"
    adapter._team_bot_user_ids.update({
        "T_ELITE": "U_ELITE_BOT", "T_CLIENT": "U_CLIENT_BOT"})
    adapter._fetch_thread_context = AsyncMock(return_value=None)
    adapter._fetch_thread_parent_text = AsyncMock(return_value="")
    adapter.handle_message = AsyncMock()
    copies = {
        team: {**_message(team, "C_SHARED", "176.001", "170.000"),
               "text": "<@U_ELITE_BOT> status?"}
        for team in ("T_ELITE", "T_CLIENT")
    }
    await adapter._handle_slack_message(copies[first_team])
    await adapter._handle_slack_message(copies[
        "T_ELITE" if first_team == "T_CLIENT" else "T_CLIENT"])
    assert adapter.handle_message.await_count == 1


@pytest.mark.asyncio
async def test_real_video_fallback_does_not_shadow_mentioned_message(monkeypatch):
    adapter = SlackAdapter(PlatformConfig(
        enabled=True, token="fake", extra={"require_mention": True}))
    adapter._app = MagicMock()
    adapter._app.client = AsyncMock()
    adapter._bot_user_id = "U_ELITE_BOT"
    adapter._team_bot_user_ids.update({
        "T_ELITE": "U_ELITE_BOT", "T_CLIENT": "U_CLIENT_BOT"})
    adapter._fetch_thread_context = AsyncMock(return_value=None)
    adapter._fetch_thread_parent_text = AsyncMock(return_value="")
    adapter._resolve_user_is_bot = AsyncMock(return_value=False)
    adapter.handle_message = AsyncMock()
    client = AsyncMock()
    client.files_info.return_value = {
        "ok": True,
        "file": {"id": "F_SHARED", "mimetype": "video/mp4", "user": "U_CLIENT",
                 "shares": {"public": {"C_SHARED": [
                     {"ts": "177.001", "thread_ts": "170.000"}]}}},
    }
    adapter._team_clients["T_CLIENT"] = client
    monkeypatch.setattr("plugins.platforms.slack.adapter.asyncio.sleep", AsyncMock())

    await adapter._handle_slack_file_shared({
        "channel_id": "C_SHARED", "file_id": "F_SHARED", "team_id": "T_CLIENT"})
    assert adapter.handle_message.await_count == 0
    mentioned = _message("T_ELITE", "C_SHARED", "177.001", "170.000")
    mentioned.update({"text": "<@U_ELITE_BOT> video?", "subtype": "file_share"})
    await adapter._handle_slack_message(mentioned)
    assert adapter.handle_message.await_count == 1
