"""Protected DNC group transport and one-send policy on the live v0.23.0 layout."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from gateway import client_reply_gate as gate
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult, _thread_metadata_for_event
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource

DNC = "C0BP7GGCEAK"
PROMPT = gate.CLIENT_PROMPT_MARKER + " Short human client reply."


def _event(ts="1731729660.000100", *, prompt=PROMPT, team="T_PRIMARY"):
    # Captured live DNC shape: SessionSource.chat_type is group and thread_id is
    # the root, while message_id is the individual inbound message timestamp.
    return MessageEvent(
        text="Please send the export", message_id=ts, channel_prompt=prompt,
        source=SessionSource(platform=Platform.SLACK, chat_id=DNC, chat_type="group",
                             thread_id="1731729000.000001", scope_id=team),
    )


@pytest.fixture
def isolated_gate(monkeypatch, tmp_path):
    alerts = []

    async def alert(channel, reason):
        alerts.append((channel, reason))

    monkeypatch.setattr(gate, "_claim_path", lambda: tmp_path / "claims.sqlite3")
    monkeypatch.setattr(gate, "_alert_sender", alert)
    gate._permits.clear()
    yield alerts
    gate._permits.clear()


def _meta(event, permit):
    return {**_thread_metadata_for_event(event), "client_reply_permit": permit}


def test_captured_dnc_group_is_protected_even_when_prompt_missing():
    assert gate.is_client_reply_channel(_event().source, PROMPT)
    assert gate.is_client_reply_channel(_event(prompt=None).source, None)
    assert _thread_metadata_for_event(_event())["client_reply_inbound_ts"] == "1731729660.000100"
    assert _thread_metadata_for_event(_event())["client_reply_chat_type"] == "group"
    assert _thread_metadata_for_event(_event())["thread_id"] == "1731729000.000001"


@pytest.mark.parametrize("text,reason", [
    (" ".join(["word"] * 61), "over_60_words"),
    ("What happened? What next?", "multiple_questions"),
    ("Thanks. Drafting the client Slack reply now.", "work_narration"),
    ("See ELI-3483 and /opt/data/config.yaml", "internal_detail"),
    ("Here's the CSV export.", "missing_attachment"),
])
def test_final_text_rejections(text, reason):
    assert gate.final_reply_violation(text, has_attachment=False) == reason


def test_unstaged_attachment_is_rejected(tmp_path):
    path = tmp_path / "missing.csv"
    assert gate.attachment_violation([], [], [str(path)]) == "attachment_not_staged"
    assert gate.prepare_client_reply(channel_id=DNC, inbound_ts="1", channel_prompt=PROMPT,
                                     text="The CSV is attached.", file_path=str(path))[1] == "attachment_not_staged"


@pytest.mark.asyncio
async def test_one_claim_ignores_team_and_root_but_allows_next_inbound(isolated_gate):
    text = "I have the figures and will send an update today."
    event = _event(team="T_FIRST")
    token, reason = gate.prepare_client_reply(channel_id=DNC, inbound_ts=event.message_id,
                                              channel_prompt=PROMPT, text=text)
    assert reason is None
    first = await gate.authorize_slack_egress(channel_id=DNC, bot_id="U_LEGEND", operation="text",
                                               text=text, metadata=_meta(event, token))
    assert first.allowed
    repeated = _event(team="T_SECOND")
    second_token, _ = gate.prepare_client_reply(channel_id=DNC, inbound_ts=repeated.message_id,
                                                channel_prompt=PROMPT, text=text)
    duplicate = await gate.authorize_slack_egress(channel_id=DNC, bot_id="U_LEGEND", operation="text",
                                                   text=text, metadata=_meta(repeated, second_token))
    assert duplicate == gate.EgressDecision(False, "duplicate_reply")
    followup = _event(ts="1731729661.000200", team="T_SECOND")
    next_token, _ = gate.prepare_client_reply(channel_id=DNC, inbound_ts=followup.message_id,
                                              channel_prompt=PROMPT, text=text)
    third = await gate.authorize_slack_egress(channel_id=DNC, bot_id="U_LEGEND", operation="text",
                                               text=text, metadata=_meta(followup, next_token))
    assert third.allowed
    assert isolated_gate == [(DNC, "duplicate_reply")]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["edit", "draft", "audio"])
async def test_other_outbound_surfaces_fail_closed(operation, isolated_gate):
    result = await gate.authorize_slack_egress(channel_id=DNC, bot_id="U_LEGEND",
                                                operation=operation, text="Hello")
    assert result == gate.EgressDecision(False, "outbound_surface_blocked")
    assert isolated_gate == [(DNC, "outbound_surface_blocked")]


@pytest.mark.asyncio
async def test_file_caption_requires_staged_matching_file(isolated_gate, tmp_path):
    path = tmp_path / "export.csv"
    path.write_text("name,total\nA,1\n")
    event = _event()
    text = "The CSV export is attached."
    token, reason = gate.prepare_client_reply(channel_id=DNC, inbound_ts=event.message_id,
                                              channel_prompt=PROMPT, text=text, file_path=str(path))
    assert reason is None
    missing = await gate.authorize_slack_egress(channel_id=DNC, bot_id="U_LEGEND", operation="text",
                                                 text=text, metadata=_meta(event, token))
    assert missing == gate.EgressDecision(False, "permit_operation_mismatch")
    token, _ = gate.prepare_client_reply(channel_id=DNC, inbound_ts=event.message_id,
                                         channel_prompt=PROMPT, text=text, file_path=str(path))
    uploaded = await gate.authorize_slack_egress(channel_id=DNC, bot_id="U_LEGEND", operation="file",
                                                  text=text, file_path=str(path), metadata=_meta(event, token))
    assert uploaded.allowed


@pytest.mark.asyncio
async def test_missing_prompt_and_non_client_bypass(isolated_gate):
    event = _event(prompt=None)
    decision = await gate.authorize_slack_egress(channel_id=DNC, bot_id="U_LEGEND", operation="text",
                                                  text="Hello", metadata=_thread_metadata_for_event(event))
    assert decision == gate.EgressDecision(False, "missing_channel_prompt")
    bypass = await gate.authorize_slack_egress(channel_id="C_INTERNAL", bot_id="U_LEGEND",
                                                operation="audio", text="Hello")
    assert bypass.allowed


class _Adapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True), Platform.SLACK)
        self.sent = []

    async def connect(self, *, is_reconnect=False):
        return True

    async def disconnect(self):
        return None

    async def get_chat_info(self, chat_id):
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append((content, metadata))
        return SendResult(success=True, message_id="m1")


@pytest.mark.asyncio
async def test_narration_final_never_reaches_ledger_or_tts(isolated_gate):
    adapter = _Adapter()
    adapter._message_handler = AsyncMock(return_value="Thanks. Drafting the client Slack reply now.")
    adapter.send_final_ledgered = AsyncMock(side_effect=AssertionError("ledger send called"))
    adapter._wants_auto_tts = lambda *a: (_ for _ in ()).throw(AssertionError("TTS ran"))
    event = _event()
    key = "agent:main:slack:group:" + DNC
    adapter._active_sessions[key] = asyncio.Event()
    await adapter._process_message_background(event, key)
    assert adapter.sent == []
    assert isolated_gate == [(DNC, "work_narration")]


@pytest.mark.asyncio
async def test_client_file_uses_one_captioned_upload_with_no_prior_text(isolated_gate, tmp_path):
    path = tmp_path / "export.csv"
    path.write_text("name,total\nA,1\n")
    adapter = _Adapter()
    adapter._record_delivery_obligation = AsyncMock(return_value=None)
    uploads = []

    async def upload(**kwargs):
        uploads.append(kwargs)
        return SendResult(success=False, error="upload refused")

    adapter.send_document = AsyncMock(side_effect=upload)
    event = _event()
    text = "The CSV export is attached."
    token, reason = gate.prepare_client_reply(channel_id=DNC, inbound_ts=event.message_id,
                                              channel_prompt=PROMPT, text=text, file_path=str(path))
    assert reason is None
    result, _ = await adapter.send_final_ledgered(
        event, "session", text, _meta(event, token), reply_to=event.source.thread_id,
        has_attachment=True, file_path=str(path))
    assert not result.success
    assert len(uploads) == 1
    assert uploads[0]["caption"] == text
    assert uploads[0]["file_path"] == str(path)
    assert adapter.sent == []
