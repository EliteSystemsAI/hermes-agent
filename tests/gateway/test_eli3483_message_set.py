"""Offline safety check for the eight captured client messages, without model or Slack I/O.

This is a message set, not the required answer-quality replay. The prompt is
a hand-written sample; this test does not load the live SOUL, skills or channel
overrides and does not call the Slack adapter. Seven original sessions have no
recorded tool-result rows.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.client_reply_finalizer import finalize_client_turn
from gateway.client_reply_gate import CLIENT_CHANNEL_IDS
from gateway.config import Platform
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionSource


FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "eli3483_client_messages.json").read_text())
MESSAGES = FIXTURE["messages"]
SAMPLE_CHANNEL_PROMPT = (
    "CLIENT COMMUNICATION STANDARD. Answer the actual question first in everyday words. "
    "One natural reply, usually 1-3 short sentences. More detail only when asked, still plain English. "
    "Say only the verified result, what is not finished, and any genuinely necessary client action. "
    "Never dump implementation details, code, branches, commits, internal ticket IDs, logs, provider/model "
    "failures or approval/access mechanics. Technical words used by a client do not make this an engineering chat. "
    "Do not ask clients to manage our internal work or repeat approvals. Keep engineering evidence private. "
    "Do not claim done/live from code or tests alone. If uncertain, say what is unconfirmed without inventing "
    "an answer or a future commitment. No robotic headers or em dashes. Answer direct questions; silence "
    "rules only apply to unsolicited noise. This applies to every client, not just Charlie."
)


def test_message_set_preserves_known_and_unknown_source_fields():
    assert FIXTURE["kind"] == "captured_message_set"
    assert len(MESSAGES) == 8
    assert len({(row["channel_id"], row["message_ts"]) for row in MESSAGES}) == 8
    assert all(row["source"]["reply_anchor_ts"] == row["thread_parent"] for row in MESSAGES)
    assert all(row["source"]["attachment_metadata"] is None for row in MESSAGES)
    assert MESSAGES[0]["source"]["observed_delivery_team_ids"] == ["T09KH5NV5SA", "T0BQMT61XHR"]
    assert MESSAGES[3]["source"]["user_id"] == "U09KT2Z9YN7"
    assert MESSAGES[6]["source"]["user_id"] == "U0C5CMDLB2P"
    assert MESSAGES[6]["source"]["routing_id"] is None


@pytest.mark.parametrize("row", MESSAGES, ids=[row["case"] for row in MESSAGES])
def test_message_without_verified_handoff_cannot_reach_final_delivery(row):
    source = SessionSource(
        platform=Platform.SLACK, chat_id=row["channel_id"],
        chat_type=row["source"]["chat_type"] or "group",
        scope_id=row["source"]["workspace_team_id"],
        thread_id=row["thread_parent"],
    )
    assert source.chat_id in CLIENT_CHANNEL_IDS
    ctx = SimpleNamespace(
        source=source, channel_prompt=SAMPLE_CHANNEL_PROMPT,
        inbound_message_id=row["message_ts"], client_message=row["text"],
        context_prompt="Legend profile context",
    )
    runner = SimpleNamespace(_ctx=ctx, _runner=SimpleNamespace(
        _get_system_prompt_for_channel=lambda *args, **kwargs: ""))
    prompt = TurnRunner._combined_ephemeral_prompt(runner)
    assert SAMPLE_CHANNEL_PROMPT in prompt
    assert f'source_channel to "{row["channel_id"]}"' in prompt
    assert f'inbound_ts to "{row["message_ts"]}"' in prompt

    class NoModel:
        def _get_transport(self):
            raise AssertionError("No provider call without a verified handoff")

    result = finalize_client_turn(
        NoModel(), {"final_response": "Unverified old answer", "messages": []}, ctx, 0)
    assert result["failed"] is True
    assert result["final_response"] == ""
    assert result["client_reply_finalizer_block"] == "missing_structured_handoff"
