"""Real v0.23.0 SlackAdapter plus final reply gate, without a Slack network call."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway import client_reply_gate as gate
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import _thread_metadata_for_event
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from plugins.platforms.slack.adapter import SlackAdapter

DNC = "C0BP7GGCEAK"
PROMPT = gate.CLIENT_PROMPT_MARKER + " Answer as a human."


@pytest.fixture
def joint(monkeypatch, tmp_path):
    alerts = []

    async def alert(channel, reason):
        alerts.append((channel, reason))

    monkeypatch.setattr(gate, "_claim_path", lambda: tmp_path / "claims.sqlite3")
    monkeypatch.setattr(gate, "_alert_sender", alert)
    gate._permits.clear()
    adapter = SlackAdapter(PlatformConfig(enabled=True, token="fake"))
    adapter._app = MagicMock()
    adapter._app.client = AsyncMock()
    adapter._app.client.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "172.001"})
    adapter._bot_user_id = "U_LEGEND"
    adapter._team_bot_user_ids["T_ELITE"] = "U_LEGEND"
    adapter._team_clients["T_ELITE"] = adapter._app.client
    yield adapter, alerts
    gate._permits.clear()


def metadata(ts, *, team="T_ELITE", text="I'll send the update today.", file_path=None):
    event = MessageEvent(
        text="Please update me", message_id=ts, channel_prompt=PROMPT,
        source=SessionSource(platform=Platform.SLACK, chat_id=DNC, chat_type="group",
                             scope_id=team, thread_id="170.000"),
    )
    token, reason = gate.prepare_client_reply(channel_id=DNC, inbound_ts=ts,
                                              channel_prompt=PROMPT, text=text,
                                              file_path=file_path)
    assert reason is None
    return {**_thread_metadata_for_event(event), "client_reply_permit": token}


@pytest.mark.asyncio
async def test_real_adapter_one_raw_post_then_duplicate_across_workspace(joint):
    adapter, alerts = joint
    text = "I'll send the update today."
    first = await adapter.send(DNC, text, metadata=metadata("171.001", text=text))
    assert first.success
    assert adapter._app.client.chat_postMessage.await_count == 1
    assert adapter._app.client.chat_postMessage.await_args.kwargs["text"] == text
    assert "blocks" not in adapter._app.client.chat_postMessage.await_args.kwargs
    adapter._team_bot_user_ids["T_OTHER"] = "U_OTHER_ELITE"
    duplicate = await adapter.send(DNC, text, metadata=metadata("171.001", team="T_OTHER", text=text))
    assert not duplicate.success
    assert duplicate.error == "duplicate_reply"
    assert adapter._app.client.chat_postMessage.await_count == 1
    assert alerts == [(DNC, "duplicate_reply")]


@pytest.mark.asyncio
async def test_real_adapter_rejects_edit_draft_file_and_voice(joint, tmp_path):
    adapter, alerts = joint
    document = tmp_path / "export.csv"
    document.write_text("name,total\nA,1\n")
    audio = tmp_path / "reply.mp3"
    audio.write_bytes(b"audio")
    meta = metadata("171.001")
    assert not (await adapter.edit_message(DNC, "172.001", "Changed", metadata=meta)).success
    assert not (await adapter.send_draft(DNC, 1, "Partial", metadata=meta)).success
    assert not (await adapter.send_document(DNC, str(document), caption="The CSV is attached.",
                                            metadata=meta)).success
    assert not (await adapter.send_voice(DNC, str(audio), metadata=meta)).success
    assert [reason for _channel, reason in alerts] == [
        "outbound_surface_blocked", "outbound_surface_blocked",
        "permit_text_mismatch", "outbound_surface_blocked"]
    adapter._app.client.chat_update.assert_not_awaited()
    adapter._app.client.files_upload_v2.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_adapter_one_captioned_file_upload(joint, tmp_path):
    adapter, alerts = joint
    path = tmp_path / "export.csv"
    path.write_text("name,total\nA,1\n")
    text = "The CSV is attached."
    adapter._app.client.files_upload_v2 = AsyncMock(return_value={"ok": True})
    result = await adapter.send_document(
        DNC, str(path), caption=text,
        metadata=metadata("171.002", text=text, file_path=str(path)))
    assert result.success
    assert adapter._app.client.files_upload_v2.await_count == 1
    assert adapter._app.client.files_upload_v2.await_args.kwargs["initial_comment"] == text
    adapter._app.client.chat_postMessage.assert_not_awaited()
    assert alerts == []
