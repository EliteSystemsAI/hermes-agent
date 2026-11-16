"""Source-bound handoff and separate no-tools finalizer for client Slack replies.

The tool-using agent must return a JSON ``client_reply_handoff``. Ordinary prose is
never promoted to verified facts. Every cited fact is an exact excerpt of the
current client message or a tool result marked successful by the executor. The
provider sees only this checked packet and no tool schema. Any failure needs a
human; it produces no client text.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from gateway.client_reply_gate import (
    DOCUMENT_SUFFIXES, detail_requested_from_message, is_client_reply_channel,
)


class ClientFinalizerBlocked(ValueError):
    """A source, packet, provider, or candidate cannot be trusted for client egress."""


def _text(value: Any, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ClientFinalizerBlocked("invalid_packet_text")
    return value.strip()


def _successful_sources(messages: list, history_offset: int) -> dict[str, str]:
    sources: dict[str, str] = {}
    for message in messages[history_offset:]:
        if (not isinstance(message, dict) or message.get("role") != "tool"
                or message.get("_client_reply_tool_succeeded") is not True):
            continue
        call_id, content = message.get("tool_call_id"), message.get("content")
        if isinstance(call_id, str) and isinstance(content, str):
            sources[call_id] = content
    return sources


def _evidence(value: Any, sources: dict[str, str], client_message: str) -> dict:
    if not isinstance(value, dict):
        raise ClientFinalizerBlocked("invalid_evidence")
    ref = _text(value.get("source_ref"), 160)
    quote = _text(value.get("quote"), 500)
    source = client_message if ref == "client_message" else sources.get(ref)
    if not source or quote not in source:
        raise ClientFinalizerBlocked("unverified_evidence")
    return {"source_ref": ref, "quote": quote}


def _optional_evidence(value: Any, sources: dict[str, str], client_message: str) -> dict | None:
    return None if value is None else _evidence(value, sources, client_message)


def build_client_reply_packet(raw: str, *, channel_id: str, inbound_ts: str | None,
                              client_message: str | None, messages: list,
                              history_offset: int) -> tuple[dict, str | None]:
    """Validate an explicit intake handoff against this turn's source rows and staged file."""
    if not inbound_ts or not client_message:
        raise ClientFinalizerBlocked("missing_inbound_identity")
    try:
        outer = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ClientFinalizerBlocked("missing_structured_handoff") from exc
    handoff = outer.get("client_reply_handoff") if isinstance(outer, dict) else None
    if not isinstance(handoff, dict):
        raise ClientFinalizerBlocked("missing_structured_handoff")
    if handoff.get("source_channel") != channel_id or str(handoff.get("inbound_ts")) != str(inbound_ts):
        raise ClientFinalizerBlocked("handoff_identity_mismatch")
    sources = _successful_sources(messages, history_offset)
    facts = handoff.get("facts")
    if not isinstance(facts, list) or len(facts) > 8:
        raise ClientFinalizerBlocked("invalid_facts")
    packet = {
        "source_channel": channel_id,
        "inbound_ts": str(inbound_ts),
        "requested_answer": _text(client_message, 3000),
        "facts": [_evidence(fact, sources, client_message) for fact in facts],
        "next_action": _optional_evidence(handoff.get("next_action"), sources, client_message),
        "confirmed_time": _optional_evidence(handoff.get("confirmed_time"), sources, client_message),
        "detail_requested": detail_requested_from_message(client_message),
        "attachment": None,
    }
    manifest = handoff.get("attachment")
    file_path = None
    if manifest is not None:
        if not isinstance(manifest, dict):
            raise ClientFinalizerBlocked("invalid_attachment_manifest")
        path = Path(_text(manifest.get("path"), 1024))
        ref = _text(manifest.get("source_ref"), 160)
        if (ref not in sources or str(path) not in sources[ref]
                or not path.is_absolute() or path.suffix.lower() not in DOCUMENT_SUFFIXES):
            raise ClientFinalizerBlocked("unverified_attachment")
        try:
            size = path.stat().st_size if path.is_file() else 0
        except OSError as exc:
            raise ClientFinalizerBlocked("attachment_not_staged") from exc
        if size <= 0 or size != manifest.get("size"):
            raise ClientFinalizerBlocked("attachment_not_staged")
        declared_type = _text(manifest.get("type"), 80)
        if declared_type != path.suffix.lower().lstrip("."):
            raise ClientFinalizerBlocked("attachment_type_mismatch")
        packet["attachment"] = {"name": path.name, "type": declared_type, "size": size}
        file_path = str(path.resolve())
    return packet, file_path


_SYSTEM = (
    "You are a client reply finalizer. You have no tools. Use only the supplied source quotes. "
    "If they do not support an answer, return {\"need_human\":true}. Otherwise return JSON "
    "{\"need_human\":false,\"text\":\"...\"}. Write one short human reply, at most 60 words "
    "unless detail_requested is true, at most one question, no internal IDs, paths, tool names, "
    "or work narration. Mention the attached file only if the manifest has one."
)


def _no_tools_candidate(agent: Any, packet: dict) -> str:
    """Use the current provider transport without exposing tools or executing tool calls."""
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": json.dumps(packet, ensure_ascii=False, separators=(",", ":"))},
    ]
    mode = str(getattr(agent, "api_mode", "") or "")
    transport = agent._get_transport()
    if mode == "anthropic_messages":
        kwargs = transport.build_kwargs(
            model=agent.model, messages=messages, tools=None, max_tokens=256,
            reasoning_config=agent.reasoning_config, is_oauth=agent._is_anthropic_oauth,
            preserve_dots=agent._anthropic_preserve_dots(),
            base_url=getattr(agent, "_anthropic_base_url", None))
    elif mode == "bedrock_converse":
        # Bedrock embeds tools in toolConfig, not a top-level tools key. Build a
        # fresh Converse request so no agent tool schema reaches boto3.
        kwargs = transport.build_kwargs(
            model=agent.model, messages=messages, tools=None, max_tokens=256,
            region=getattr(agent, "_bedrock_region", None) or "us-east-1",
            guardrail_config=getattr(agent, "_bedrock_guardrail_config", None),
            reasoning_config=getattr(agent, "reasoning_config", None))
    else:
        kwargs = agent._build_api_kwargs(messages)
        kwargs.pop("tools", None)
        kwargs.pop("parallel_tool_calls", None)
        kwargs.pop("tool_choice", None)
        if mode != "codex_responses":
            kwargs["tool_choice"] = "none"
    if mode != "bedrock_converse":
        kwargs["timeout"] = 20
    response = agent._interruptible_api_call(kwargs)
    normalized = transport.normalize_response(
        response, **({"strip_tool_prefix": agent._is_anthropic_oauth}
                    if mode == "anthropic_messages" else {}))
    if normalized.tool_calls:
        raise ClientFinalizerBlocked("finalizer_attempted_tool_call")
    return _text(normalized.content, 4000)


def finalize_client_turn(agent: Any, result: dict, ctx: Any, history_offset: int) -> dict:
    """Return the result with one validated candidate or a closed human handoff."""
    if not is_client_reply_channel(ctx.source, ctx.channel_prompt):
        return result
    try:
        if result.get("failed") or result.get("interrupted"):
            raise ClientFinalizerBlocked("intake_failed")
        packet, file_path = build_client_reply_packet(
            result.get("final_response"), channel_id=ctx.source.chat_id,
            inbound_ts=ctx.inbound_message_id, client_message=ctx.client_message,
            messages=result.get("messages") or [], history_offset=history_offset)
        response = _no_tools_candidate(agent, packet)
        try:
            candidate = json.loads(response)
        except ValueError as exc:
            raise ClientFinalizerBlocked("invalid_finalizer_candidate") from exc
        if (not isinstance(candidate, dict) or set(candidate) != {"need_human", "text"}
                or candidate.get("need_human") is not False):
            raise ClientFinalizerBlocked("need_human")
        text = _text(candidate.get("text"), 4000)
        result["client_reply_final_text"] = text
        if file_path:
            text += f"\nMEDIA: {file_path}"
        result["final_response"] = text
        result["client_reply_finalized"] = True
        result["client_reply_manifest_file_path"] = file_path
        result["client_reply_detail_requested"] = packet["detail_requested"]
    except Exception as exc:
        reason = str(exc) if isinstance(exc, ClientFinalizerBlocked) else "finalizer_unavailable"
        result["final_response"] = ""
        result["failed"] = True
        result["client_reply_finalizer_block"] = reason
    return result
