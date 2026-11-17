"""Tests for the once-only SIGTERM retry. Run with the patched cron package first on sys.path."""
import os, sys, tempfile, time, threading
from pathlib import Path

import cron.jobs as jobs
import cron.scheduler_script as ss

tmp = Path(tempfile.mkdtemp())
jobs.CRON_DIR = tmp
scripts = tmp / "scripts"
scripts.mkdir()
res = []


def check(name, cond):
    res.append(cond)
    print(("PASS " if cond else "FAIL ") + name)


def script(name, body):
    p = scripts / name
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(0o755)
    return str(p)


# Call the real runner, but point path resolution at our temp script.
ss._resolve_script_path = lambda p: (Path(p), None)

count_file = tmp / "count"


def killed_once():
    count_file.write_text("0")
    return script("k.sh", f'n=$(cat {count_file}); echo $((n+1)) > {count_file}\n'
                         f'if [ "$n" = 0 ]; then kill -TERM $$; fi\necho ran\n')


def runs():
    return int(count_file.read_text())


allow = tmp / "retry_on_sigterm.txt"

# 1. listed job, SIGTERM at start: retried once, succeeds
allow.write_text("# safe to re-run\njobA  # comment\n")
ok, out = ss._run_job_script_with_claim_heartbeat({"id": "jobA"}, killed_once())
check("listed job retried once and succeeds", ok and out == "ran" and runs() == 2)

# 2. job not listed: no retry, failure kept
ok, out = ss._run_job_script_with_claim_heartbeat({"id": "jobB"}, killed_once())
check("unlisted job not retried", (not ok) and out.startswith("Script exited with code -15") and runs() == 1)

# 3. no allowlist file: no retry
allow.unlink()
ok, out = ss._run_job_script_with_claim_heartbeat({"id": "jobA"}, killed_once())
check("missing allowlist means no retry", (not ok) and runs() == 1)
allow.write_text("jobA\n")

# 4. killed every time: exactly one retry, then failure returned
count_file.write_text("0")
always = script("a.sh", f'n=$(cat {count_file}); echo $((n+1)) > {count_file}\nkill -TERM $$\n')
ok, out = ss._run_job_script_with_claim_heartbeat({"id": "jobA"}, always)
check("retries at most once", (not ok) and runs() == 2)

# 5. other non-zero exit: no retry
count_file.write_text("0")
one = script("e.sh", f'n=$(cat {count_file}); echo $((n+1)) > {count_file}\nexit 1\n')
ok, out = ss._run_job_script_with_claim_heartbeat({"id": "jobA"}, one)
check("exit 1 not retried", (not ok) and runs() == 1)

# 6. SIGTERM after the window: no retry
ss._SIGTERM_RETRY_WINDOW_SECONDS = 0.5
count_file.write_text("0")
late = script("l.sh", f'n=$(cat {count_file}); echo $((n+1)) > {count_file}\nsleep 1\nkill -TERM $$\n')
ok, out = ss._run_job_script_with_claim_heartbeat({"id": "jobA"}, late)
check("SIGTERM after the window not retried", (not ok) and runs() == 1)
ss._SIGTERM_RETRY_WINDOW_SECONDS = 15.0

# 7. cancelled run: no retry (scheduler cancel writes its own message anyway)
ev = threading.Event()
count_file.write_text("0")
slow = script("s.sh", f'n=$(cat {count_file}); echo $((n+1)) > {count_file}\nsleep 5\n')
threading.Timer(0.5, ev.set).start()
ok, out = ss._run_job_script_with_claim_heartbeat({"id": "jobA"}, slow, cancel_event=ev)
check("cancelled run not retried", (not ok) and out.startswith("Script cancelled") and runs() == 1)

# 8. success path unchanged
ok, out = ss._run_job_script_with_claim_heartbeat({"id": "jobA"}, script("g.sh", "echo hi\n"))
check("plain success unchanged", ok and out == "hi")

print(f"pass={sum(res)} fail={len(res) - sum(res)}")
sys.exit(0 if all(res) else 1)
