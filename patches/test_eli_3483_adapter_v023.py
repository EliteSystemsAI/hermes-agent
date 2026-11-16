"""The Slack transport must consult the client reply gate before any visible write."""

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.slack.adapter import SlackAdapter


@pytest.fixture
def guarded_adapter(monkeypatch):
    gate = ModuleType("gateway.client_reply_gate")
    gate.CLIENT_CHANNEL_IDS = frozenset({"C_SHARED"})
    gate.authorize_slack_egress = AsyncMock(
        return_value=SimpleNamespace(allowed=False, reason="no_permit"))
    monkeypatch.setitem(sys.modules, "gateway.client_reply_gate", gate)
    adapter = SlackAdapter(PlatformConfig(enabled=True, token="fake"))
    adapter._app = MagicMock()
    adapter._app.client = AsyncMock()
    adapter._bot_user_id = "U_LEGEND"
    adapter._team_bot_user_ids["T_ELITE"] = "U_LEGEND"
    metadata = {
        "slack_team_id": "T_ELITE", "thread_id": "170.000",
        "client_reply_inbound_ts": "171.001", "client_reply_chat_type": "group",
        "client_reply_channel_prompt": "CLIENT COMMUNICATION STANDARD.",
    }
    return adapter, gate.authorize_slack_egress, metadata


@pytest.mark.asyncio
async def test_text_edit_and_draft_deny_before_slack_api(guarded_adapter):
    adapter, gate, metadata = guarded_adapter
    assert not (await adapter.send("C_SHARED", "Final answer", metadata=metadata)).success
    assert not (await adapter.edit_message(
        "C_SHARED", "172.001", "Changed answer", metadata=metadata)).success
    assert not (await adapter.send_draft(
        "C_SHARED", 1, "Partial answer", metadata=metadata)).success
    assert [call.kwargs["operation"] for call in gate.await_args_list] == [
        "text", "edit", "draft"]
    assert all(call.kwargs["bot_id"] == "U_LEGEND" for call in gate.await_args_list)
    assert all(call.kwargs["metadata"]["client_reply_chat_type"] == "group"
               for call in gate.await_args_list)
    adapter._app.client.chat_postMessage.assert_not_awaited()
    adapter._app.client.chat_update.assert_not_awaited()
    adapter._app.client.chat_startStream.assert_not_awaited()


@pytest.mark.asyncio
async def test_file_and_audio_deny_before_upload(guarded_adapter, tmp_path):
    adapter, gate, metadata = guarded_adapter
    document = tmp_path / "report.pdf"
    document.write_bytes(b"pdf")
    audio = tmp_path / "reply.mp3"
    audio.write_bytes(b"audio")
    assert not (await adapter.send_document(
        "C_SHARED", str(document), caption="Here is the report", metadata=metadata)).success
    assert not (await adapter.send_voice(
        "C_SHARED", str(audio), metadata=metadata)).success
    assert [call.kwargs["operation"] for call in gate.await_args_list] == [
        "file", "audio"]
    assert gate.await_args_list[0].kwargs["text"] == "Here is the report"
    assert gate.await_args_list[0].kwargs["file_path"] == str(document)
    adapter._app.client.files_upload_v2.assert_not_awaited()


@pytest.mark.asyncio
async def test_allowed_file_uses_one_upload_with_exact_initial_comment(guarded_adapter, tmp_path):
    adapter, gate, metadata = guarded_adapter
    gate.return_value = SimpleNamespace(allowed=True, reason=None)
    adapter._app.client.files_upload_v2 = AsyncMock(return_value={"ok": True})
    document = tmp_path / "report.pdf"
    document.write_bytes(b"pdf")

    result = await adapter.send_document(
        "C_SHARED", str(document), caption="Here is the report", metadata=metadata)

    assert result.success
    gate.assert_awaited_once()
    assert gate.await_args.kwargs["operation"] == "file"
    assert gate.await_args.kwargs["text"] == "Here is the report"
    adapter._app.client.files_upload_v2.assert_awaited_once()
    assert adapter._app.client.files_upload_v2.await_args.kwargs["initial_comment"] == "Here is the report"
    adapter._app.client.chat_postMessage.assert_not_awaited()


@pytest.mark.asyncio
async def test_gate_exception_fails_closed(guarded_adapter):
    adapter, gate, metadata = guarded_adapter
    gate.side_effect = RuntimeError("gate unavailable")

    result = await adapter.send("C_SHARED", "Final answer", metadata=metadata)

    assert not result.success
    assert result.error == "client_reply_gate_error"
    adapter._app.client.chat_postMessage.assert_not_awaited()


@pytest.mark.asyncio
async def test_protected_text_is_one_raw_unformatted_post(guarded_adapter):
    adapter, gate, metadata = guarded_adapter
    gate.return_value = SimpleNamespace(allowed=True, reason=None)
    adapter._app.client.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "172.001"})
    raw = "Here is *the answer*."

    result = await adapter.send("C_SHARED", raw, metadata=metadata)

    assert result.success
    assert gate.await_args.kwargs["text"] == raw
    adapter._app.client.chat_postMessage.assert_awaited_once()
    sent = adapter._app.client.chat_postMessage.await_args.kwargs
    assert sent["text"] == raw
    assert "blocks" not in sent


@pytest.mark.asyncio
async def test_internal_alert_notifies_ej_and_zac_dms(guarded_adapter):
    adapter, _, _ = guarded_adapter
    internal = AsyncMock()
    internal.chat_postMessage.return_value = {"ok": True, "ts": "180.001"}
    internal.conversations_open.return_value = {
        "ok": True, "channel": {"id": "D_ZAC"}}
    adapter._team_clients["T09KH5NV5SA"] = internal

    await adapter._send_client_reply_alert("C_SHARED", "missing_permit")

    assert [call.kwargs["channel"] for call in internal.chat_postMessage.await_args_list] == [
        "D0BJHG8SWN8", "D_ZAC"]
    internal.conversations_open.assert_awaited_once_with(users="U09KCRR9TGT")


@pytest.mark.asyncio
async def test_failed_ej_dm_retries_then_alerts_zac(guarded_adapter):
    adapter, _, _ = guarded_adapter
    internal = AsyncMock()
    internal.chat_postMessage.side_effect = [
        RuntimeError("EJ DM unavailable"), RuntimeError("EJ DM unavailable"),
        {"ok": True, "ts": "180.003"}]
    internal.conversations_open.return_value = {
        "ok": True, "channel": {"id": "D_ZAC"}}
    adapter._team_clients["T09KH5NV5SA"] = internal

    await adapter._send_client_reply_alert("C_SHARED", "missing_permit")

    assert [call.kwargs["channel"] for call in internal.chat_postMessage.await_args_list] == [
        "D0BJHG8SWN8", "D0BJHG8SWN8", "D_ZAC"]
    assert "Please notify EJ directly" in internal.chat_postMessage.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_unconfirmed_zac_dm_is_not_reported_as_delivery(guarded_adapter):
    adapter, _, _ = guarded_adapter
    internal = AsyncMock()
    internal.chat_postMessage.side_effect = [
        {"ok": False}, {"ok": False}, {"ok": False}]
    internal.conversations_open.return_value = {
        "ok": True, "channel": {"id": "D_ZAC"}}
    adapter._team_clients["T09KH5NV5SA"] = internal

    with pytest.raises(RuntimeError, match="Zac DM delivery"):
        await adapter._send_client_reply_alert("C_SHARED", "missing_permit")


@pytest.mark.asyncio
async def test_claim_identity_comes_from_authenticated_bot(guarded_adapter):
    adapter, gate, metadata = guarded_adapter
    metadata["client_reply_bot_id"] = "U_OTHER_BOT"

    await adapter.send("C_SHARED", "Final answer", metadata=metadata)

    assert gate.await_args.kwargs["bot_id"] == ""
