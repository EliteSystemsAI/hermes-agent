"""Opt-in, no-send egress safety replay on live Legend prompts plus PR 4 adapter.

Run on a joint PR 2, PR 3 and PR 4 checkout with
HERMES_TEST_SSH_HOST=hermes-cloud scripts/run_tests.sh
tests/gateway/test_eli3483_live_adapter_replay.py -q.
This does not call a model or prove answer quality.
"""

import hashlib
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway import client_reply_gate as gate
from gateway.client_reply_finalizer import finalize_client_turn
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import _thread_metadata_for_event
from gateway.platforms.event import MessageEvent
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionSource
from plugins.platforms.slack.adapter import SlackAdapter


READY = (os.environ.get("HERMES_TEST_SSH_HOST") == "hermes-cloud"
         and hasattr(SlackAdapter, "_authorize_client_reply_transport"))
pytestmark = pytest.mark.skipif(not READY, reason="needs opt-in live profile read and stacked PR 4 adapter")
MESSAGES = json.loads((Path(__file__).parent / "fixtures" /
                      "eli3483_client_messages.json").read_text())["messages"]


@pytest.fixture(scope="module")
def live_prompt_stack():
    remote = '''
import json
from pathlib import Path
root = Path("/opt/data/profiles/default")
soul = (root / "SOUL.md").read_text()
config_lines = (root / "config.yaml").read_text().splitlines()
channels = {"C0BP7GGCEAK", "C09KQBU5RPW", "C09KLRPP1PC", "C09KEPSR0Q3"}
prompts = {}
for line in config_lines:
    for channel in channels:
        prefix = "    " + channel + ": "
        if line.startswith(prefix):
            prompts[channel] = line[len(prefix):]
skills = sorted(str(p.relative_to(root)) for p in (root / "skills").glob("**/SKILL.md"))
print(json.dumps({"soul": soul, "prompts": prompts, "skills": skills}))
'''
    proc = subprocess.run(
        ["ssh", "hermes-cloud", "docker exec -i --user hermes hermes python -"],
        input=remote, text=True, capture_output=True, check=True, timeout=20)
    stack = json.loads(proc.stdout)
    assert hashlib.sha256(stack["soul"].encode()).hexdigest() == (
        "883f6c0228801dec651dcd655f80d8e85526fb9f06c0a0c39be84e4652ad04b2")
    assert len(stack["prompts"]) == 4
    assert stack["skills"] == []
    return stack


@pytest.fixture
def no_send_adapter(monkeypatch, tmp_path):
    alerts = []

    async def alert(channel, reason):
        alerts.append((channel, reason))

    monkeypatch.setattr(gate, "_claim_path", lambda: tmp_path / "claims.sqlite3")
    monkeypatch.setattr(gate, "_alert_sender", alert)
    gate._permits.clear()
    adapter = SlackAdapter(PlatformConfig(enabled=True, token="fake"))
    adapter._app = MagicMock()
    adapter._app.client = AsyncMock()
    adapter._bot_user_id = "U_LEGEND"
    yield adapter, alerts
    gate._permits.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("row", MESSAGES, ids=[row["case"] for row in MESSAGES])
async def test_old_unverified_answer_is_held_before_actual_adapter_send(no_send_adapter, live_prompt_stack, row):
    """Read live prompt files, then prove no Slack API call for unsupported old prose."""
    adapter, alerts = no_send_adapter
    source_meta = row["source"]
    # Two captured messages lack workspace and routing records. This assumed
    # team is only for the local adapter test; the fixture keeps those null.
    team = source_meta["workspace_team_id"] or "T_ELITE_TEST"
    adapter._team_clients[team] = adapter._app.client
    adapter._team_bot_user_ids[team] = "U_LEGEND"
    event = MessageEvent(
        text=row["text"], message_id=row["message_ts"],
        channel_prompt=live_prompt_stack["prompts"][row["channel_id"]],
        source=SessionSource(
            platform=Platform.SLACK, chat_id=row["channel_id"],
            chat_type=source_meta["chat_type"] or "group", scope_id=team,
            thread_id=row["thread_parent"]),
    )
    ctx = SimpleNamespace(
        source=event.source, channel_prompt=event.channel_prompt,
        inbound_message_id=event.message_id, client_message=event.text, context_prompt="")
    runner = SimpleNamespace(_ctx=ctx, _runner=SimpleNamespace(
        _get_system_prompt_for_channel=lambda *args, **kwargs: ""))
    full_prompt = live_prompt_stack["soul"] + "\n\n" + TurnRunner._combined_ephemeral_prompt(runner)
    assert event.channel_prompt in full_prompt
    assert "CLIENT REPLY HANDOFF" in full_prompt

    class NoModel:
        def _get_transport(self):
            raise AssertionError("No provider call without a verified handoff")

    final = finalize_client_turn(
        NoModel(), {"final_response": "Unverified old answer", "messages": []}, ctx, 0)
    assert final["failed"] and final["final_response"] == ""
    assert final["client_reply_finalizer_block"] == "missing_structured_handoff"
    result = await adapter.send(
        row["channel_id"], "Unverified old answer", metadata=_thread_metadata_for_event(event))
    assert not result.success
    assert result.error == "missing_or_used_permit"
    adapter._app.client.chat_postMessage.assert_not_awaited()
    assert alerts == [(row["channel_id"], "missing_or_used_permit")]
