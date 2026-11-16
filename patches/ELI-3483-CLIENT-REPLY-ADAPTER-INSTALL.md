# ELI-3483 client reply adapter patch for Hermes 0.23.0

This is the adapter part of the reply gate. Install it only after the PR 2
dedupe patch and the PR 3 gate patch are reviewed and installed together.
Do not restart between the patches. The adapter patch is based on exact
upstream v0.23.0 commit
`2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19` plus the PR 2 dedupe
patch. It is third-party gateway image code.

The combined check patch contains dedupe plus adapter changes and passed a
read-only `git apply --check` against the real live 0.23.0 adapter as the
`hermes` user. The incremental patch passed
`git apply --check --cached` against the exact v0.23.0 tag with dedupe staged.
PR 3's gate patch does not edit the Slack adapter.

Run this block from this PR checkout only after PR 2 has been installed.
It verifies the expected post-dedupe checksum, makes a backup, applies the
incremental patch as `hermes` to a staged copy, and lets root install that
copy because `/opt/hermes` is root-owned. Any failed step stops the block.
There is no client test send. The coordinator performs one gateway restart
after the whole gate stack is installed in a quiet window.

```sh
set -eu
patch=patches/eli-3483-client-reply-adapter-v0.23.0.patch
target=/opt/hermes/plugins/platforms/slack/adapter.py
backup=/opt/data/adapter.py.bak-ELI-3483-before-reply-gate-v0.23.0
stage=/opt/data/eli-3483-reply-gate-adapter-stage
before=9d2b37b2a421d8729426a77ad63cf8002b65bacb9869af800878338ebae6cc24
after=ee35bcaac0d82bb321782306f684bd35108d6f4eda5bc781535222e84c2e70fb

ssh hermes-cloud 'docker exec -i -u hermes -e HOME=/opt/data hermes sh -lc "cd /opt/hermes && git apply --check -"' < "$patch"
ssh hermes-cloud "docker exec hermes sha256sum $target" | grep -Fx "$before  $target"
ssh hermes-cloud "docker exec hermes sh -ec 'test ! -e $backup && cp -p $target $backup'"
ssh hermes-cloud "docker exec hermes sha256sum $backup" | grep -Fx "$before  $backup"
ssh hermes-cloud "docker exec -i -u hermes -e HOME=/opt/data hermes sh -ec 'test ! -e $stage && mkdir -p $stage/plugins/platforms/slack && cp $target $stage/plugins/platforms/slack/adapter.py && cd $stage && git apply -'" < "$patch"
ssh hermes-cloud "docker exec hermes sha256sum $stage/plugins/platforms/slack/adapter.py" | grep -Fx "$after  $stage/plugins/platforms/slack/adapter.py"
ssh hermes-cloud "docker exec hermes sha256sum $target" | grep -Fx "$before  $target"
ssh hermes-cloud "docker exec hermes install -o root -g root -m 644 $stage/plugins/platforms/slack/adapter.py $target"
ssh hermes-cloud "docker exec hermes sha256sum $target" | grep -Fx "$after  $target"
```

After the one coordinated restart, verify the next organic client message
produces one agent run and one short final reply, and that a second message
in the same thread gets its own run. For a blocked reply, confirm Legend
notified EJ in DM `D0BJHG8SWN8` and Zac in his DM. If EJ's DM fails twice,
confirm Zac received the fallback asking him to notify EJ. A stored alert
without confirmed Slack delivery is not proof. Do not send a synthetic
client message.

Rollback of this adapter file is valid only while the image is still 0.23.0
and no subsequent adapter change was installed. Restore the client-channel
Legend mute before removing this enforcement, then coordinate the PR 3 gate
rollback and one gateway restart. Check the backup checksum first:

```sh
set -eu
backup=/opt/data/adapter.py.bak-ELI-3483-before-reply-gate-v0.23.0
target=/opt/hermes/plugins/platforms/slack/adapter.py
before=9d2b37b2a421d8729426a77ad63cf8002b65bacb9869af800878338ebae6cc24
ssh hermes-cloud "docker exec hermes sha256sum $backup" | grep -Fx "$before  $backup"
ssh hermes-cloud "docker exec hermes cp -p $backup $target"
```

A container recreation or image update replaces this writable-layer edit.
Rebase and review the patch against any later image version.
