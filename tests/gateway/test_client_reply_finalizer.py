"""Explicit evidence handoff and provider-boundary no-tools finalizer."""

import json
from types import SimpleNamespace

import pytest

from gateway.client_reply_finalizer import (
    ClientFinalizerBlocked, _no_tools_candidate, build_client_reply_packet, finalize_client_turn,
)
from gateway.client_reply_gate import CLIENT_PROMPT_MARKER
from gateway.config import Platform
from gateway.session import SessionSource

DNC = "C0BP7GGCEAK"
TS = "171.001"
QUESTION = "Can you send the tender total?"


def _handoff(**changes):
    packet = {
        "source_channel": DNC, "inbound_ts": TS,
        "facts": [{"source_ref": "tool_1", "quote": "Tender total is $540"}],
        "next_action": None, "confirmed_time": None, "attachment": None,
    }
    packet.update(changes)
    return json.dumps({"client_reply_handoff": packet})


def _tool(*, success=True, content="Tender total is $540"):
    return {"role": "tool", "tool_call_id": "tool_1", "content": content,
            "_client_reply_tool_succeeded": success}


def _packet(raw=None, messages=None):
    return build_client_reply_packet(
        raw if raw is not None else _handoff(), channel_id=DNC, inbound_ts=TS,
        client_message=QUESTION, messages=[_tool()] if messages is None else messages,
        history_offset=0)


def test_only_successful_exact_source_quotes_enter_packet():
    packet, path = _packet()
    assert packet["facts"] == [{"source_ref": "tool_1", "quote": "Tender total is $540"}]
    assert packet["requested_answer"] == QUESTION
    assert path is None
    with pytest.raises(ClientFinalizerBlocked, match="unverified_evidence"):
        _packet(messages=[_tool(success=False)])
    with pytest.raises(ClientFinalizerBlocked, match="unverified_evidence"):
        _packet(raw=_handoff(facts=[{"source_ref": "tool_1", "quote": "Tender total is $900"}]))
    with pytest.raises(ClientFinalizerBlocked, match="missing_structured_handoff"):
        _packet(raw="The tender total is $540.")
    with pytest.raises(ClientFinalizerBlocked, match="unverified_evidence"):
        build_client_reply_packet(_handoff(), channel_id=DNC, inbound_ts=TS,
                                  client_message=QUESTION, messages=[_tool()], history_offset=1)


def test_attachment_requires_successful_source_and_exact_staged_manifest(tmp_path):
    path = tmp_path / "tender.csv"
    path.write_text("total\n540\n")
    manifest = {"path": str(path), "type": "csv", "size": path.stat().st_size,
                "source_ref": "tool_1"}
    packet, chosen = _packet(
        raw=_handoff(attachment=manifest),
        messages=[_tool(content=f"Tender total is $540; saved {path}")])
    assert packet["attachment"] == {"name": "tender.csv", "type": "csv", "size": path.stat().st_size}
    assert chosen == str(path.resolve())
    with pytest.raises(ClientFinalizerBlocked, match="attachment_not_staged"):
        _packet(raw=_handoff(attachment={**manifest, "size": 999}),
                messages=[_tool(content=f"Tender total is $540; saved {path}")])


class _Agent:
    api_mode = "codex_responses"
    model = "test"

    def __init__(self, response='{"need_human":false,"text":"The tender total is $540."}', tool_calls=None):
        self.response = response
        self.tool_calls = tool_calls or []
        self.kwargs = None

    def _get_transport(self):
        return self

    def _build_api_kwargs(self, messages):
        return {"messages": messages, "tools": [{"name": "terminal"}],
                "tool_choice": "auto", "parallel_tool_calls": True}

    def _interruptible_api_call(self, kwargs):
        self.kwargs = kwargs
        return object()

    def normalize_response(self, _response, **_kwargs):
        return SimpleNamespace(content=self.response, tool_calls=self.tool_calls)


def _ctx():
    return SimpleNamespace(
        source=SessionSource(platform=Platform.SLACK, chat_id=DNC, chat_type="group"),
        channel_prompt=CLIENT_PROMPT_MARKER, inbound_message_id=TS, client_message=QUESTION)


def test_finalizer_provider_call_has_no_tools_and_one_candidate():
    agent = _Agent()
    result = {"final_response": _handoff(), "messages": [_tool()]}
    finalized = finalize_client_turn(agent, result, _ctx(), 0)
    assert finalized["client_reply_finalized"] is True
    assert finalized["final_response"] == "The tender total is $540."
    assert "tools" not in agent.kwargs
    assert "tool_choice" not in agent.kwargs
    assert "parallel_tool_calls" not in agent.kwargs
    assert agent.kwargs["timeout"] == 20


def test_finalizer_tool_attempt_or_missing_packet_needs_human():
    agent = _Agent(tool_calls=[object()])
    result = finalize_client_turn(agent, {"final_response": _handoff(), "messages": [_tool()]}, _ctx(), 0)
    assert result["failed"] and result["final_response"] == ""
    assert result["client_reply_finalizer_block"] == "finalizer_attempted_tool_call"
    agent = _Agent()
    result = finalize_client_turn(agent, {"final_response": "ordinary prose", "messages": [_tool()]}, _ctx(), 0)
    assert result["failed"] and result["final_response"] == ""
    assert agent.kwargs is None


def test_chat_completion_finalizer_forces_tool_choice_none():
    agent = _Agent()
    agent.api_mode = "chat_completions"
    result = finalize_client_turn(agent, {"final_response": _handoff(), "messages": [_tool()]}, _ctx(), 0)
    assert result["client_reply_finalized"] is True
    assert "tools" not in agent.kwargs
    assert agent.kwargs["tool_choice"] == "none"


def test_legend_client_prompt_includes_source_bound_handoff_contract():
    from gateway.run_turn_runner import TurnRunner

    ctx = _ctx()
    ctx.context_prompt = "Legend live profile context"
    ctx.channel_prompt = ("CLIENT COMMUNICATION STANDARD. Answer the actual question first "
                          "in everyday words. One natural reply, usually 1-3 short sentences.")
    ctx.source.thread_id = "171.000"
    runner = SimpleNamespace(_ctx=ctx, _runner=SimpleNamespace(
        _get_system_prompt_for_channel=lambda *args, **kwargs: "DNC channel override"))
    prompt = TurnRunner._combined_ephemeral_prompt(runner)
    assert "Legend live profile context" in prompt
    assert ctx.channel_prompt in prompt
    assert "DNC channel override" in prompt
    assert "CLIENT REPLY HANDOFF" in prompt
    assert 'source_channel to "C0BP7GGCEAK"' in prompt
    assert 'inbound_ts to "171.001"' in prompt
    assert "successful tool result from this turn" in prompt
    assert "prior turns, failed tools" in prompt
    assert "The gateway, not you" in prompt


def test_bedrock_finalizer_builds_real_converse_kwargs_without_tools():
    from agent.transports.bedrock import BedrockTransport

    class BedrockAgent(_Agent):
        api_mode = "bedrock_converse"
        model = "anthropic.claude-3-5-sonnet-20241022-v2:0"
        _bedrock_region = "ap-southeast-2"
        _bedrock_guardrail_config = None
        reasoning_config = None

        def __init__(self):
            super().__init__()
            self.transport = BedrockTransport()
            self.transport.normalize_response = lambda response: SimpleNamespace(
                content='{"need_human":false,"text":"The tender total is $540."}', tool_calls=None)

        def _get_transport(self):
            return self.transport

        def _build_api_kwargs(self, messages):
            raise AssertionError("agent builder would inherit its toolConfig")

    agent = BedrockAgent()
    candidate = _no_tools_candidate(agent, {"facts": []})
    assert json.loads(candidate)["need_human"] is False
    assert agent.kwargs["__bedrock_region__"] == "ap-southeast-2"
    assert agent.kwargs["__bedrock_converse__"] is True
    assert "toolConfig" not in agent.kwargs
    assert "tools" not in agent.kwargs
    assert "tool_choice" not in agent.kwargs
    assert "timeout" not in agent.kwargs
