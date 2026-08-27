"""Tests for the ``transform_llm_output`` plugin hook.

The hook fires inside ``AIAgent.run_conversation`` once the tool-calling
loop has produced a final response. Driving the full agent loop from a
unit test would be prohibitively heavy, so these tests exercise the
invoke_hook dispatch semantics that the wiring in ``run_agent.py``
depends on:

    for _hook_result in _transform_results:
        if isinstance(_hook_result, str) and _hook_result:
            final_response = _hook_result
            break  # First non-empty string wins

Mirrors ``test_transform_tool_result_hook.py`` which tests the equivalent
contract for the generic tool-result seam.
"""

import inspect
from pathlib import Path

import yaml

import hermes_cli.plugins as plugins_mod
from hermes_cli.plugins import PluginManager, VALID_HOOKS


def test_runtime_forwards_stable_route_and_turn_metadata_to_plugin_hooks():
    """Gateway plugins must not have to infer a topic from a transcript UUID."""
    from agent.turn_context import build_turn_context
    from agent.turn_finalizer import finalize_turn

    pre_source = inspect.getsource(build_turn_context)
    final_source = inspect.getsource(finalize_turn)

    assert 'gateway_session_key=getattr(agent, "_gateway_session_key"' in pre_source
    transform_block = final_source.split(
        '"transform_llm_output"', 1
    )[1].split("# Plugin hook: post_llm_call", 1)[0]
    for field in (
        "task_id=effective_task_id",
        "turn_id=turn_id",
        "user_message=original_user_message",
        'gateway_session_key=getattr(agent, "_gateway_session_key"',
    ):
        assert field in transform_block


def _make_enabled_plugin(hermes_home: Path, name: str, register_body: str) -> Path:
    """Create a plugin under <hermes_home>/plugins/<name> and opt it in."""
    plugin_dir = hermes_home / "plugins" / name
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        yaml.safe_dump({"name": name, "version": "0.1.0"}), encoding="utf-8",
    )
    (plugin_dir / "__init__.py").write_text(
        "def register(ctx):\n"
        f"    {register_body}\n",
        encoding="utf-8",
    )
    cfg_path = hermes_home / "config.yaml"
    cfg = {}
    if cfg_path.exists():
        cfg = yaml.safe_load(cfg_path.read_text()) or {}
    cfg.setdefault("plugins", {}).setdefault("enabled", []).append(name)
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return plugin_dir


def test_transform_llm_output_in_valid_hooks():
    assert "transform_llm_output" in VALID_HOOKS


def test_hook_receives_expected_kwargs(tmp_path, monkeypatch):
    """Hook callback should see response_text + session_id + model + platform."""
    hermes_home = tmp_path / "hermes_test"
    hermes_home.mkdir(exist_ok=True)
    _make_enabled_plugin(
        hermes_home, "capture_hook",
        register_body=(
            'ctx.register_hook("transform_llm_output", '
            'lambda **kw: f"{kw[\'response_text\']}|{kw[\'session_id\']}|'
            '{kw[\'model\']}|{kw[\'platform\']}")'
        ),
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    mgr = PluginManager()
    mgr.discover_and_load()

    results = mgr.invoke_hook(
        "transform_llm_output",
        response_text="hello world",
        session_id="s1",
        model="anthropic/claude-sonnet-4.6",
        platform="cli",
    )
    assert results == ["hello world|s1|anthropic/claude-sonnet-4.6|cli"]






def test_hook_exception_does_not_replace_response(tmp_path, monkeypatch):
    """A plugin raising an exception must not break hook dispatch.

    PluginManager.invoke_hook catches per-callback exceptions, logs a
    warning, and continues — so a raising plugin contributes no entry
    to the results list, and the walk in run_agent.py finds nothing to
    replace with.
    """
    hermes_home = tmp_path / "hermes_test"
    hermes_home.mkdir(exist_ok=True)
    _make_enabled_plugin(
        hermes_home, "raising_hook",
        register_body=(
            'def _boom(**kw):\n'
            '        raise RuntimeError("boom")\n'
            '    ctx.register_hook("transform_llm_output", _boom)'
        ),
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    mgr = PluginManager()
    mgr.discover_and_load()

    results = mgr.invoke_hook(
        "transform_llm_output",
        response_text="keep me",
        session_id="s1",
        model="m",
        platform="cli",
    )

    final_response = "keep me"
    for _hook_result in results:
        if isinstance(_hook_result, str) and _hook_result:
            final_response = _hook_result
            break

    assert final_response == "keep me"


def test_no_plugins_returns_empty_results(tmp_path, monkeypatch):
    """With no plugins loaded, invoke_hook returns [] and the response is unchanged."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes_empty"))
    plugins_mod._plugin_manager = PluginManager()

    mgr = plugins_mod._plugin_manager
    results = mgr.invoke_hook(
        "transform_llm_output",
        response_text="unchanged",
        session_id="",
        model="m",
        platform="",
    )
    assert results == []
