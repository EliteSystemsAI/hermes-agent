"""Fail-closed outbound policy for the client Slack channels in CLIENT-DELIVERY.md."""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from hermes_constants import get_process_hermes_home

logger = logging.getLogger(__name__)
CLIENT_PROMPT_MARKER = "CLIENT COMMUNICATION STANDARD."
CLIENT_CHANNEL_IDS = frozenset({
    # Exact Legend client mute list: 26 root prompts plus archived GreenScope.
    "C09KLRPP1PC", "C09KEPSR0Q3", "C09KL0ZN8H1", "C09KNPLC4BZ",
    "C09KQBU5RPW", "C09KQNUUSJG", "C09KVV4GFNF", "C09L0PRTMT7",
    "C09L0Q5GRFX", "C09L25R8KN2", "C09L7PVFW6Q", "C09L7QQSLH2",
    "C09LFGTFRGQ", "C09LH57UJTT", "C09LY0U6MK6", "C09UAMLNT6Z",
    "C0A5RHHK6EP", "C0A5XUHTXMG", "C0A5XUNJWHY", "C0A61GPAWJG",
    "C0AB32RE5DW", "C0AE4UGK01J", "C0BCK3G1G5S", "C0BCRG8001G",
    "C0BG8HQMYHH", "C0BP7GGCEAK", "C0BCDP18Z46",
})
DOCUMENT_SUFFIXES = frozenset({".csv", ".pdf", ".txt", ".docx", ".xlsx"})
_INTERNAL = re.compile(
    r"\b(?:ELI-\d+|kanban|Linear|ACP|tool[ -]?call|shell|terminal|"
    r"config\.ya?ml|SOUL\.md|session[_ -]?key|stack trace|traceback|"
    r"webhook|API endpoint|job\s*(?:id|number)\s*[:#]?\s*\d+)\b|"
    r"\b[CDGU][A-Z0-9]{10,}\b|"
    r"(?:/(?:opt|home|tmp|var)/[^\s]+)|"
    r"(?<!\w)(?:\.{1,2}/|[A-Za-z0-9_.-]+/)[A-Za-z0-9_./-]+\.[A-Za-z0-9]{1,8}\b",
    re.IGNORECASE,
)
_NARRATION = re.compile(
    r"\b(?:drafting (?:the |a |your )?(?:client )?(?:Slack )?reply|"
    r"retrying (?:skill|tool|model)|loading (?:skill|tool)|"
    r"checking (?:my |the )?(?:tools|logs|internal status)|"
    r"running (?:a |the )?(?:check|tool|command)|"
    r"switching to|using (?:the |a )?(?:local |ACP |shell |tool)|"
    r"(?:i am|i'm|we are|we're) (?:checking|running|working|retrying|loading)|"
    r"redirected current run|interrupting current task|"
    r"(?:the )?(?:current )?(?:run|task) (?:was |is )?(?:redirected|interrupted))\b",
    re.IGNORECASE,
)
_FILE_CLAIM = re.compile(
    r"\b(?:attached|attachment|enclosed|uploaded)\b|"
    r"\b(?:here(?:'s| is)|sent|sending)\b[^.!?\n]{0,80}\b"
    r"(?:file|csv|export|report|document|spreadsheet)\b", re.IGNORECASE,
)


def is_client_reply_channel(source: Any, channel_prompt: str | None = None) -> bool:
    """Explicit channel IDs govern activation; the prompt is separately required."""
    platform = getattr(source, "platform", None)
    return (getattr(platform, "value", platform) == "slack"
            and getattr(source, "chat_id", None) in CLIENT_CHANNEL_IDS)


def final_reply_violation(text: str, *, has_attachment: bool,
                          detail_requested: bool = False) -> str | None:
    if not text or not text.strip():
        return "empty_reply"
    if not detail_requested and len(text.split()) > 60:
        return "over_60_words"
    if text.count("?") > 1:
        return "multiple_questions"
    if "\u2014" in text or "\u2013" in text:
        return "dash_punctuation"
    if _NARRATION.search(text):
        return "work_narration"
    if _INTERNAL.search(text) or "```" in text:
        return "internal_detail"
    if _FILE_CLAIM.search(text) and not has_attachment:
        return "missing_attachment"
    if has_attachment and not _FILE_CLAIM.search(text):
        return "attachment_not_mentioned"
    return None


_DETAIL_REQUEST = re.compile(
    r"\b(?:in detail|detailed (?:answer|breakdown|explanation)|full (?:details|breakdown|explanation)|"
    r"step[ -]by[ -]step|all (?:the )?details|explain (?:everything|fully))\b",
    re.IGNORECASE,
)


def detail_requested_from_message(message: str | None) -> bool:
    """Only an explicit request in this inbound message relaxes the 60 word limit."""
    return bool(_DETAIL_REQUEST.search(message or ""))


def attachment_violation(images: list, media_files: list, local_files: list) -> str | None:
    if images:
        return "image_attachment"
    paths = [path for path, _is_voice in media_files] + list(local_files)
    if len(paths) > 1:
        return "multiple_attachments"
    if any(is_voice for _path, is_voice in media_files):
        return "voice_attachment"
    if paths and Path(paths[0]).suffix.lower() not in DOCUMENT_SUFFIXES:
        return "non_document_attachment"
    if paths and not _staged_file(paths[0]):
        return "attachment_not_staged"
    return None


def _staged_file(path: str | Path | None) -> bool:
    try:
        p = Path(path) if path else None
        return bool(p and p.is_file() and p.stat().st_size > 0)
    except (OSError, TypeError, ValueError):
        return False


@dataclass(frozen=True)
class EgressDecision:
    allowed: bool
    reason: str | None = None


@dataclass(frozen=True)
class _Permit:
    channel_id: str
    inbound_ts: str
    text_digest: str
    file_path: str | None


_permits: dict[str, _Permit] = {}
_permit_lock = threading.Lock()
_alert_sender: Callable[[str, str], Awaitable[None]] | None = None


def configure_client_reply_alert_sender(sender: Callable[[str, str], Awaitable[None]]) -> None:
    """Install internal EJ and Zac alert sender with Zac fallback; never target a client."""
    global _alert_sender
    _alert_sender = sender


async def alert_client_reply_block(channel_id: str, reason: str) -> None:
    logger.error("Client Slack reply blocked for %s: %s", channel_id, reason)
    if _alert_sender is None:
        logger.critical("Client reply alert sender unconfigured; human alert not delivered")
        return
    try:
        await _alert_sender(channel_id, reason)
    except Exception:
        logger.exception("Client reply alert failed; Zac fallback is required")


def prepare_client_reply(*, channel_id: str, inbound_ts: str | None, channel_prompt: str | None,
                         text: str, file_path: str | None = None,
                         detail_requested: bool = False) -> tuple[str | None, str | None]:
    """Validate the complete candidate and issue an opaque one-use permit."""
    if channel_id not in CLIENT_CHANNEL_IDS:
        return None, "unprotected_channel"
    if not inbound_ts:
        return None, "missing_inbound_ts"
    if CLIENT_PROMPT_MARKER not in (channel_prompt or ""):
        return None, "missing_channel_prompt"
    if file_path and Path(file_path).suffix.lower() not in DOCUMENT_SUFFIXES:
        return None, "non_document_attachment"
    if file_path and not _staged_file(file_path):
        return None, "attachment_not_staged"
    reason = final_reply_violation(text, has_attachment=bool(file_path),
                                   detail_requested=detail_requested)
    if reason:
        return None, reason
    token = secrets.token_urlsafe(32)
    with _permit_lock:
        _permits[token] = _Permit(channel_id, str(inbound_ts),
                                  hashlib.sha256(text.encode()).hexdigest(),
                                  str(Path(file_path).resolve()) if file_path else None)
    return token, None


def _claim_path() -> Path:
    return get_process_hermes_home() / "client_reply_claims.sqlite3"


def _claim_once(channel_id: str, inbound_ts: str) -> bool:
    path = _claim_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path, timeout=5) as db:
        db.execute("CREATE TABLE IF NOT EXISTS sent_v2 (channel_id TEXT NOT NULL, "
                   "inbound_ts TEXT NOT NULL, PRIMARY KEY(channel_id,inbound_ts))")
        cur = db.execute("INSERT OR IGNORE INTO sent_v2 VALUES (?,?)",
                         (channel_id, inbound_ts))
        return cur.rowcount == 1


async def authorize_slack_egress(*, channel_id: str, bot_id: str, operation: str,
                                  text: str = "", file_path: str | None = None,
                                  metadata: dict | None = None) -> EgressDecision:
    """One send-boundary check for Slack text, edit, draft, file and audio transports."""
    if channel_id not in CLIENT_CHANNEL_IDS:
        return EgressDecision(True)
    meta = metadata or {}
    reason = None
    if _alert_sender is None:
        reason = "alert_sender_unconfigured"
    elif operation not in {"text", "file"}:
        reason = "outbound_surface_blocked"
    elif meta.get("client_reply_chat_type") not in {"group", "channel"}:
        reason = "invalid_chat_type"
    elif CLIENT_PROMPT_MARKER not in str(meta.get("client_reply_channel_prompt") or ""):
        reason = "missing_channel_prompt"
    elif not bot_id:
        reason = "missing_bot_id"
    elif not meta.get("client_reply_inbound_ts"):
        reason = "missing_inbound_ts"
    else:
        token = meta.get("client_reply_permit")
        with _permit_lock:
            permit = _permits.pop(token, None) if isinstance(token, str) else None
        if permit is None:
            reason = "missing_or_used_permit"
        elif permit.channel_id != channel_id or permit.inbound_ts != str(meta["client_reply_inbound_ts"]):
            reason = "permit_identity_mismatch"
        elif permit.text_digest != hashlib.sha256(text.encode()).hexdigest():
            reason = "permit_text_mismatch"
        elif (permit.file_path is None) != (operation == "text"):
            reason = "permit_operation_mismatch"
        elif operation == "file" and (not file_path or
                str(Path(file_path).resolve()) != permit.file_path or not _staged_file(file_path)):
            reason = "permit_file_mismatch"
        else:
            try:
                if not _claim_once(channel_id, permit.inbound_ts):
                    reason = "duplicate_reply"
            except (OSError, sqlite3.Error):
                logger.exception("Client reply claim failed")
                reason = "claim_unavailable"
    if reason:
        await alert_client_reply_block(channel_id, reason)
        return EgressDecision(False, reason)
    return EgressDecision(True)
