# ELI-3483 live image patch

The running gateway is Hermes Agent 0.23.0 from Docker image
`nousresearch/hermes-agent:latest`. It imports
`/opt/hermes/plugins/platforms/slack/adapter.py`, which is root-owned and is
not a git checkout. The upstream v0.23.0 tag is
`2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19`. Its adapter SHA256
matches the live file:
`19ff264f300a2e352f99df01b84d25b196a4f6e5852599f0403823b9922f2f47`.

For independent regression review, use a scratch checkout of exact upstream
tag `v0.23.0` at the commit above. Apply this PR's
`eli-3483-v0.23.0.patch` and `eli-3483-v0.23.0-tests.patch` there, then
run `PYTHONPATH=. uvx --python 3.11 --from pytest --with pytest-asyncio
--with ruamel.yaml --with aiohttp --with psutil pytest
tests/gateway/test_slack_connect_dedup_023.py -q`. The test patch adds the
test to that tag checkout, so imports resolve to the actual v0.23.0 adapter,
not the fork main tree. The six tag tests passed locally.

Apply only after independent review and in a quiet window. Do not send a test
message to a client. Run the block from this PR checkout. It stops on any
failed check or backup. The patch is applied to a staged copy as the
`hermes` user, then root installs the staged file because the image source is
root-owned. No patch command runs as root.

```sh
set -eu
patch=patches/eli-3483-v0.23.0.patch
target=/opt/hermes/plugins/platforms/slack/adapter.py
backup=/opt/data/adapter.py.bak-ELI-3483-v0.23.0
stage=/opt/data/eli-3483-stage
sha=19ff264f300a2e352f99df01b84d25b196a4f6e5852599f0403823b9922f2f47
after=8cd9c51dcf03423b9a81049d8f2bda35405dd45bab1a405633eab64aeaa4d977

ssh hermes-cloud 'docker exec -i -u hermes -e HOME=/opt/data hermes sh -lc "cd /opt/hermes && git apply --check -"' < "$patch"
ssh hermes-cloud "docker exec hermes sha256sum $target" | grep -Fx "$sha  $target"
ssh hermes-cloud "docker exec hermes sh -ec 'test ! -e $backup && cp -p $target $backup'"
ssh hermes-cloud "docker exec hermes sha256sum $backup" | grep -Fx "$sha  $backup"
ssh hermes-cloud "docker exec -i -u hermes -e HOME=/opt/data hermes sh -ec 'test ! -e $stage && mkdir -p $stage/plugins/platforms/slack && cp $target $stage/plugins/platforms/slack/adapter.py && cd $stage && git apply -'" < "$patch"
ssh hermes-cloud "docker exec -u hermes -e HOME=/opt/data hermes python -c 'import ast; ast.parse(open(\"$stage/plugins/platforms/slack/adapter.py\").read())'"
ssh hermes-cloud "docker exec hermes sha256sum $stage/plugins/platforms/slack/adapter.py" | grep -Fx "$after  $stage/plugins/platforms/slack/adapter.py"
ssh hermes-cloud "docker exec hermes sha256sum $target" | grep -Fx "$sha  $target"
ssh hermes-cloud "docker exec hermes install -o root -g root -m 644 $stage/plugins/platforms/slack/adapter.py $target"
ssh hermes-cloud "docker exec hermes sha256sum $target" | grep -Fx "$after  $target"
ssh hermes-cloud 'docker exec -u hermes -e HOME=/opt/data hermes /opt/hermes/.venv/bin/python -c "import plugins.platforms.slack.adapter as a; assert hasattr(a.SlackAdapter, \"_inbound_message_dedup_id\"); print(a.__file__)"'
ssh hermes-cloud 'docker exec -u hermes -e HOME=/opt/data hermes /opt/hermes/.venv/bin/hermes -p default gateway restart --detach'
ssh hermes-cloud 'docker exec -u hermes -e HOME=/opt/data hermes /opt/hermes/.venv/bin/hermes -p default gateway status'
```

The restart command does not force interruption of active turns. If it refuses
because work is active, wait for a quiet window and run only the restart and
status commands then. Observe the next organic message in a shared client
channel: one inbound run and one final reply for each individual message ts.
A later follow-up in the same thread must still get its own run. Check gateway
logs and Slack author IDs. No extra client message is needed.

Rollback is valid only while the image is still 0.23.0 and this dedupe patch
is the only change to the adapter since the backup. Check the backup checksum
before restoring it. Use a quiet window and restart once.

```sh
set -eu
sha=19ff264f300a2e352f99df01b84d25b196a4f6e5852599f0403823b9922f2f47
backup=/opt/data/adapter.py.bak-ELI-3483-v0.23.0
target=/opt/hermes/plugins/platforms/slack/adapter.py
ssh hermes-cloud "docker exec hermes sha256sum $backup" | grep -Fx "$sha  $backup"
ssh hermes-cloud "docker exec hermes cp -p $backup $target"
ssh hermes-cloud 'docker exec -u hermes -e HOME=/opt/data hermes /opt/hermes/.venv/bin/hermes -p default gateway restart --detach'
ssh hermes-cloud 'docker exec -u hermes -e HOME=/opt/data hermes /opt/hermes/.venv/bin/hermes -p default gateway status'
```

The edit lives only in this container's writable layer. An image update or
container recreation replaces it. The fork PR does not deploy to this image
by itself; bake the accepted change into a pinned image before the next
update, or review and reapply a version-specific patch.
