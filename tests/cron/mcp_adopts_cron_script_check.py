"""Reproduce: a cron script started while an MCP stdio server connects is adopted as an MCP child,
then killpg()'d by the post-tick orphan sweep. Uses the real modules; run with the copy under test
first on sys.path. Expect: live code kills the script (-15), fixed code leaves it alone."""
import os, sys, tempfile, threading, time
from pathlib import Path

# OVERLAY=<dir with cron/ and tools/ holding only the files under test>: put them first on the
# package paths, so nothing else has to be copied.
_ov = os.environ.get("OVERLAY")
if _ov:
    import importlib
    import cron, tools
    cron.__path__.insert(0, os.path.join(_ov, "cron"))
    tools.__path__.insert(0, os.path.join(_ov, "tools"))
    for _m in ("tools.mcp_tool_lifecycle", "tools.mcp_tool_transport", "cron.scheduler_script"):
        importlib.reload(importlib.import_module(_m))

import cron.scheduler_script as ss
from tools import mcp_tool_lifecycle as _lc_check, mcp_tool_transport as _tr_check
print("  using", ss.__file__, _lc_check.__file__, _tr_check.__file__)
from tools import mcp_tool_lifecycle as lc
from tools.mcp_tool_transport import MCPServerTransportMixin

tmp = Path(tempfile.mkdtemp())
ss._resolve_script_path = lambda p: (Path(p), None)
res = []


def check(name, cond):
    res.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name)


def script(name, body):
    p = tmp / name
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(0o755)
    return str(p)


class FakeServer:
    name = "fake-stdio"


def run_scenario(label, skip_filter=False):
    """MCP connect window opens, a cron script starts inside it, the session ends while the
    script still runs, then the post-tick sweep runs."""
    before = lc._snapshot_child_pids()                      # _run_stdio: pids_before
    out = {}
    t = threading.Thread(target=lambda: out.update(r=ss._run_job_script(script("s.sh", "sleep 4\necho done\n"))))
    t.start()
    time.sleep(0.5)                                         # script is now a direct child
    raw = lc._snapshot_child_pids() - before
    # skip_filter: the snapshot was taken before the script was registered (fork-to-register window)
    new = set(raw) if skip_filter else lc._filter_mcp_children(raw)   # _run_stdio's delta
    print(f"  [{label}] raw delta={sorted(raw)}")
    # what _track_spawned_children ledgers (without the spawn-ledger and supervisor side effects)
    with lc._core._lock:
        for pid in new:
            lc._stdio_pids[pid] = FakeServer.name
            lc._stdio_pgids[pid] = os.getpgid(pid)
            st = lc._leader_start_time(pid)
            if st is not None:
                lc._stdio_starttimes[pid] = st
    MCPServerTransportMixin._release_spawned_children(FakeServer(), set(new))   # session ended
    lc._kill_orphaned_mcp_children()                        # post-tick sweep
    t.join(15)
    ok, output = out.get("r", (False, "no result"))
    print(f"  [{label}] adopted={sorted(new)} result={ok} {output.splitlines()[0] if output else ''}")
    return new, ok, output


new, ok, output = run_scenario("race")
check("cron script started in the MCP window survives the sweep", ok and output == "done")
check("no cron script pid left in the MCP orphan ledger", not (set(new) & set(lc._orphan_stdio_pids)))

new, ok, output = run_scenario("adopted-before-registered", skip_filter=True)
check("script adopted before it was registered still survives (release and sweep checks)", ok and output == "done")
check("and it is not left in any MCP ledger", not (set(new) & (set(lc._orphan_stdio_pids) | set(lc._stdio_pgids))))

# a real MCP-style orphan (not a cron script) is still reaped as before
import subprocess
p = subprocess.Popen(["sleep", "30"], start_new_session=True)
with lc._core._lock:
    lc._orphan_stdio_pids.add(p.pid)
    lc._orphan_stdio_pid_servers[p.pid] = "fake-stdio"
    lc._stdio_pgids[p.pid] = p.pid
    st = lc._leader_start_time(p.pid)
    if st is not None:
        lc._stdio_starttimes[p.pid] = st
lc._kill_orphaned_mcp_children()
check("a genuine MCP orphan is still killed", p.wait(timeout=5) in (-15, -9))

# registry is cleared after the run
check("cron pids unregistered after the run", not getattr(lc, "_non_mcp_child_pids", set()))

print(f"pass={sum(res)} fail={len(res) - sum(res)}")
sys.exit(0 if all(res) else 1)
