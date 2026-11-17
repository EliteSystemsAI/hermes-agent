"""python run_with_overlay.py <overlay dir> <test file>: reload the files under test from the overlay, then run the test."""
import importlib, os, runpy, sys

ov, test = sys.argv[1], sys.argv[2]
import cron, tools
cron.__path__.insert(0, os.path.join(ov, "cron"))
tools.__path__.insert(0, os.path.join(ov, "tools"))
for m in ("tools.mcp_tool_lifecycle", "tools.mcp_tool_transport", "cron.scheduler_script"):
    importlib.reload(importlib.import_module(m))
print("  using", sys.modules["cron.scheduler_script"].__file__)
sys.argv = [test]
runpy.run_path(test, run_name="__main__")
