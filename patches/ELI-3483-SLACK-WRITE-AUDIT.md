# Slack write audit for the live v0.23.0 adapter

Scope: `plugins/platforms/slack/adapter.py` from upstream tag
`2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19`, with PR 2 dedupe and
this PR 4 patch. Protected means one of the 27 explicit Legend client
channel IDs in `gateway/client_reply_gate.py`.

| Slack write | Protected-channel result |
| --- | --- |
| `chat.postMessage` final text | Gate checks the exact raw text and one claim before one unchunked post. |
| `files_upload_v2` | Gate checks one permitted caption and staged file path before upload. Batch images and remote image bytes have no matching permit and are denied. |
| `chat.update`, `chat.delete` | Edit operation denied by gate before write. Model picker and approval update paths have explicit guards. |
| `chat.startStream`, `chat.appendStream`, `chat.stopStream` | Draft operation denied at entry. Direct start, seal and native task-card stop paths have protected-channel guards. |
| `chat.postEphemeral` and slash `response_url` | Protected slash context is denied in `send`; direct ephemeral fallback also checks the channel. Generic ephemeral notice calls the gate. |
| `assistant.threads.setStatus` or `agents.sessions.setStatus` | Typing refresh is not registered. `send_typing`, `_keep_typing`, status setter and status clear all no-op for protected IDs. |
| `reactions.add`, `reactions.remove` | Both return before Slack API for protected IDs. |
| `assistant.threads.setSuggestedPrompts`, `setTitle` or `agents.sessions.rename` | Protected IDs return before Slack API. |
| Standalone cron/tool `chat.postMessage` or `files_upload_v2` | Protected destination is denied before token selection or Slack API. |
| Alert `chat.postMessage` and `conversations.open` | Legend sends only to EJ's real DM and Zac's DM, with Zac DM resolution in the Elite workspace. These are internal destinations, never protected client channel IDs. |
| `pins.*`, `bookmarks.*`, `canvases.*`, scheduled messages, `conversations.mark` | No call sites in this adapter version. |

Read-only Slack calls such as `auth.test`, `users.info`, `files.info`,
`conversations.info`, and thread history do not write to the channel.
The exact-v0.23.0 transport tests call protected group-thread and
top-level `send_typing`, refresh, clear, reactions, delete, title, prompts,
ephemeral fallback and stream paths with mocked Slack clients and assert no
Slack write.
