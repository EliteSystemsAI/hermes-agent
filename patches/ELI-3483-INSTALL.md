# ELI-3483 live image patch

The running gateway is Hermes Agent 0.23.0 from Docker image
`nousresearch/hermes-agent:latest`. It imports
`/opt/hermes/plugins/platforms/slack/adapter.py`, which is root-owned and is
not a git checkout. The separate Athena profile git checkout reported by
`hermes --version` is not the running gateway source. Apply this only after
independent review and after the already pending stopgap restart. Use one more
graceful gateway restart in a quiet window. Do not send a test message to a
client.

From the local PR checkout, with `patches/eli-3483-v0.23.0.patch` available:

```sh
ssh hermes-cloud 'docker exec -i -u hermes -e HOME=/opt/data hermes sh -lc "cd /opt/hermes && git apply --check -"' < patches/eli-3483-v0.23.0.patch
ssh hermes-cloud 'docker exec hermes cp -p /opt/hermes/plugins/platforms/slack/adapter.py /opt/data/adapter.py.bak-ELI-3483-v0.23.0'
ssh hermes-cloud 'docker exec -i hermes sh -lc "cd /opt/hermes && git apply -"' < patches/eli-3483-v0.23.0.patch
ssh hermes-cloud 'docker exec -u hermes -e HOME=/opt/data hermes /opt/hermes/.venv/bin/python -c "import plugins.platforms.slack.adapter as a; assert hasattr(a.SlackAdapter, \"_inbound_message_dedup_id\"); print(a.__file__)"'
ssh hermes-cloud 'docker exec -u hermes -e HOME=/opt/data hermes /opt/hermes/.venv/bin/hermes -p default gateway restart --detach'
ssh hermes-cloud 'docker exec -u hermes -e HOME=/opt/data hermes /opt/hermes/.venv/bin/hermes -p default gateway status'
```

The restart command does not force interruption of active turns. If it refuses
because work is active, wait for a quiet window and run that command again.
After restart, observe the next organic message in each shared client channel:
one inbound run and one final reply for each individual message ts, with a
later follow-up in the same thread still answered. Check gateway logs and
Slack author IDs. No extra client message is needed for verification.

Rollback in a quiet window:

```sh
ssh hermes-cloud 'docker exec hermes cp -p /opt/data/adapter.py.bak-ELI-3483-v0.23.0 /opt/hermes/plugins/platforms/slack/adapter.py'
ssh hermes-cloud 'docker exec -u hermes -e HOME=/opt/data hermes /opt/hermes/.venv/bin/hermes -p default gateway restart --detach'
ssh hermes-cloud 'docker exec -u hermes -e HOME=/opt/data hermes /opt/hermes/.venv/bin/hermes -p default gateway status'
```

The `/opt/hermes` edit lives only in this container's writable layer. A Docker
image update or container recreation replaces it. The fork PR does not deploy
to this image by itself; bake the accepted change into a pinned image before
the next update, or review and reapply the patch against the new version.
