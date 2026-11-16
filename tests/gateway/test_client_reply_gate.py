"""Protected DNC group transport and one-send policy on the live v0.23.0 layout."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway import client_reply_gate as gate
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult, _thread_metadata_for_event
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource

DNC = "C0BP7GGCEAK"
PROMPT = gate.CLIENT_PROMPT_MARKER + " Short human client reply."
CONFIRMED_CLIENT_CHANNELS = {
    "C09KLRPP1PC", "C09KEPSR0Q3", "C09KL0ZN8H1", "C09KNPLC4BZ",
    "C09KQBU5RPW", "C09KQNUUSJG", "C09KVV4GFNF", "C09L0PRTMT7",
    "C09L0Q5GRFX", "C09L25R8KN2", "C09L7PVFW6Q", "C09L7QQSLH2",
    "C09LFGTFRGQ", "C09LH57UJTT", "C09LY0U6MK6", "C09UAMLNT6Z",
    "C0A5RHHK6EP", "C0A5XUHTXMG", "C0A5XUNJWHY", "C0A61GPAWJG",
    "C0AB32RE5DW", "C0AE4UGK01J", "C0BCK3G1G5S", "C0BCRG8001G",
    "C0BG8HQMYHH", "C0BP7GGCEAK", "C0BCDP18Z46",
}


def _event(ts="1731729660.000100", *, prompt=PROMPT, team="T_PRIMARY"):
    # Captured live DNC shape: SessionSource.chat_type is group and thread_id is
    # the root, while message_id is the individual inbound message timestamp.
    event = MessageEvent(
        text="Please send the export", message_id=ts, channel_prompt=prompt,
        source=SessionSource(platform=Platform.SLACK, chat_id=DNC, chat_type="group",
                             thread_id="1731729000.000001", scope_id=team),
    )
    event._client_reply_finalized = True
    event._client_reply_manifest_file_path = None
    return event


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


def test_client_map_matches_confirmed_legend_mute():
    assert gate.CLIENT_CHANNEL_IDS == CONFIRMED_CLIENT_CHANNELS
    assert len(gate.CLIENT_CHANNEL_IDS) == 27


@pytest.mark.parametrize("text,reason", [
    (" ".join(["word"] * 61), "over_60_words"),
    ("What happened? What next?", "multiple_questions"),
    ("Thanks. Drafting the client Slack reply now.", "work_narration"),
    ("See ELI-3483 and /opt/data/config.yaml", "internal_detail"),
    ("Here's the CSV export.", "missing_attachment"),
])
def test_final_text_rejections(text, reason):
    assert gate.final_reply_violation(text, has_attachment=False) == reason


@pytest.mark.parametrize("text", [
    "Switching to local shell to read the tender file.",
    "Using the ACP tool-call format now.",
    "The shell tool returned the totals.",
    "I saved docs/tender.csv for you.",
    "See ./exports/tender.csv.",
    "I posted this to C0BP7GGCEAK.",
    "Job id 44821 is running.",
    "I'm checking the drawings now.",
    "Redirected current run.",
    "Interrupting current task.",
    "Status: still working on it.",
    "Your request is queued for processing.",
    "Looking into that now.",
    "On it, back shortly.",
    "The tool returned 540.",
    "Job 44821 is running.",
    "The workspace is T0BQMT61XHR.",
    "I saved docs/tender for you.",
])
def test_grok_exact_internal_narration_examples_are_blocked(text):
    assert gate.final_reply_violation(text, has_attachment=False) in {
        "work_narration", "internal_detail"}


@pytest.mark.parametrize("text", [
    "The shell of the building is intact.",
    "The ACP panel is included in the drawing.",
])
def test_client_subject_words_are_not_internal_details(text):
    assert gate.final_reply_violation(text, has_attachment=False) is None


def test_explicit_detail_request_allows_long_reply():
    text = " ".join(["detail"] * 61)
    assert gate.final_reply_violation(text, has_attachment=False) == "over_60_words"
    assert gate.detail_requested_from_message("Please give me a detailed breakdown of the cost.")
    token, reason = gate.prepare_client_reply(
        channel_id=DNC, inbound_ts="171.003", channel_prompt=PROMPT, text=text,
        detail_requested=gate.detail_requested_from_message("Please give me a detailed breakdown."))
    assert token and reason is None


class _PersistenceRunner:
    from gateway.run_turn_persistence import GatewayTurnPersistenceMixin

    async def _clear_restart_failure_count(self, *_args):
        return None

    def _is_intentional_silence(self, agent_result, response):
        from gateway.response_filters import is_intentional_silence_agent_result
        return is_intentional_silence_agent_result(agent_result, response)

    def _silence_allowed_for_source(self, *_args):
        return False


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [
    {"final_response": "(empty)", "api_calls": 2},
    {"final_response": "[SILENT]", "api_calls": 1},
    {"final_response": "", "api_calls": 0, "interrupted": True},
    {"final_response": "", "api_calls": 1, "failed": True, "error": "outage"},
])
async def test_runtime_status_fallback_never_enters_client_delivery(result, isolated_gate):
    from gateway.run_turn_persistence import GatewayTurnPersistenceMixin

    class Runner(_PersistenceRunner, GatewayTurnPersistenceMixin):
        pass

    runner = Runner()
    shaped, _silent, _messages = await runner._hmwa_shape_agent_response(
        {**result, "messages": []}, _event().source, history=[],
        session_entry=SimpleNamespace(session_id="s"), session_key=None,
        _quick_key=None, run_generation=0, _run_start_session_id="s",
        _platform_name="slack", _msg_start_time=0.0, reply_expected=True)
    assert shaped == ""
    assert isolated_gate == [(DNC, "runtime_status_or_empty_final")]


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
    duplicate = await gate.authorize_slack_egress(channel_id=DNC, bot_id="U_OTHER_ELITE_WORKSPACE", operation="text",
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
    event._client_reply_final_text = "Thanks. Drafting the client Slack reply now."
    key = "agent:main:slack:group:" + DNC
    adapter._active_sessions[key] = asyncio.Event()
    await adapter._process_message_background(event, key)
    assert adapter.sent == []
    assert isolated_gate == [(DNC, "work_narration")]


@pytest.mark.asyncio
async def test_plain_intake_prose_without_no_tools_finalizer_fails_closed(isolated_gate):
    adapter = _Adapter()
    adapter._message_handler = AsyncMock(return_value="I will send the figures today.")
    adapter.send_final_ledgered = AsyncMock(side_effect=AssertionError("ledger send called"))
    event = _event()
    event._client_reply_finalized = False
    key = "agent:main:slack:group:" + DNC
    adapter._active_sessions[key] = asyncio.Event()
    await adapter._process_message_background(event, key)
    assert adapter.sent == []
    assert isolated_gate == [(DNC, "missing_no_tools_finalizer")]


@pytest.mark.asyncio
async def test_text_changed_after_finalizer_fails_closed(isolated_gate):
    adapter = _Adapter()
    adapter._message_handler = AsyncMock(return_value="The total is $540. Status: still working.")
    adapter.send_final_ledgered = AsyncMock(side_effect=AssertionError("ledger send called"))
    event = _event()
    event._client_reply_final_text = "The total is $540."
    key = "agent:main:slack:group:" + DNC
    adapter._active_sessions[key] = asyncio.Event()
    await adapter._process_message_background(event, key)
    assert adapter.sent == []
    assert isolated_gate == [(DNC, "finalizer_text_changed")]


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
