"""Integration tests for the Hermes plugin surface.

These load the actual plugin entry point (``__init__.py``) the same way Hermes does — via
``importlib`` from the plugin directory — and drive its registration and hook callbacks with a
recording context. They assert the contract Hermes relies on:

* ``register(ctx)`` registers the declared tools, hooks and the slash command.
* Hook callbacks return the shapes Hermes expects and never raise.
* The pre-LLM gate blocks only when a decision depends on an invalidated assumption.

A ``HERMES_HOME`` temp directory keeps the plugin's data out of the real profile.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def plugin(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    spec = importlib.util.spec_from_file_location("assumption_ledger_plugin", ROOT / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # Reset the module-level host so each test gets its own data directory.
    mod._HOST = mod.LedgerHost()
    return mod


class RecordingContext:
    """Minimal stand-in for the Hermes PluginContext, recording every registration."""

    def __init__(self, config=None):
        self.tools = {}
        self.hooks = {}
        self.commands = {}
        self._config = dict(config or {})

    def get_config(self, key, default=None):
        return self._config.get(key, default)

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler, **kw}

    def register_hook(self, hook, callback):
        self.hooks.setdefault(hook, []).append(callback)

    def register_command(self, name, handler, **kw):
        self.commands[name] = {"handler": handler, **kw}


def test_register_declares_expected_surface(plugin):
    ctx = RecordingContext()
    plugin.register(ctx)
    assert set(ctx.tools) == {
        "assumption_record", "assumption_check", "assumption_status", "assumption_decide",
        "assumption_resolve", "assumption_drop",
    }
    assert set(ctx.hooks) == {"pre_llm_call", "post_llm_call", "pre_verify", "post_tool_call"}
    assert "assumptions" in ctx.commands
    for tool in ctx.tools.values():
        assert tool["toolset"] == "assumption_ledger"
        assert isinstance(tool["schema"], dict)


def test_manifest_declared_tools_match_registrations(plugin):
    import re

    manifest = (ROOT / "plugin.yaml").read_text()
    declared_tools = set(re.findall(r"^\s*-\s*(assumption_\w+)$", manifest, re.MULTILINE))
    declared_hooks = set(re.findall(r"^\s*-\s*(pre_\w+|post_\w+)$", manifest, re.MULTILINE))
    ctx = RecordingContext()
    plugin.register(ctx)
    assert declared_tools == set(ctx.tools), "manifest tools must match registrations"
    assert declared_hooks == set(ctx.hooks), "manifest hooks must match registrations"


def test_tool_handlers_round_trip(plugin, tmp_path):
    ctx = RecordingContext()
    plugin.register(ctx)
    (tmp_path / "present.txt").write_text("x")

    rec = json.loads(ctx.tools["assumption_record"]["handler"](
        {"statement": "the file present.txt exists",
         "check": {"kind": "file_exists", "path": str(tmp_path / "present.txt")}}
    ))
    assert "id" in rec and rec["status"] == "UNVERIFIED"

    checked = json.loads(ctx.tools["assumption_check"]["handler"]({"id": rec["id"]}))
    assert checked["status"] == "VALID"

    status = json.loads(ctx.tools["assumption_status"]["handler"]({}))
    assert status["total"] == 1

    dec = json.loads(ctx.tools["assumption_decide"]["handler"](
        {"statement": "use present.txt", "depends_on": [rec["id"]]}
    ))
    assert dec["state"] == "open"


def test_tool_handlers_reject_bad_input(plugin):
    ctx = RecordingContext()
    plugin.register(ctx)
    assert "error" in json.loads(ctx.tools["assumption_record"]["handler"]({"statement": "x"}))
    assert "error" in json.loads(ctx.tools["assumption_record"]["handler"]({"check": {"kind": "file_exists"}}))
    assert "error" in json.loads(ctx.tools["assumption_decide"]["handler"]({"statement": "x", "depends_on": "nope"}))


def test_post_llm_call_captures_and_pre_llm_reinjects(plugin):
    ctx = RecordingContext()
    plugin.register(ctx)
    plugin.on_post_llm_call(assistant_response="ASSUMPTION: the file a.txt exists", session_id="s1")
    out = plugin.on_pre_llm_call(session_id="s1")
    assert isinstance(out, dict) and "context" in out
    assert "a.txt" in out["context"]


def test_pre_llm_warns_but_never_blocks(plugin, tmp_path):
    """pre_llm_call must never veto: the agent needs the turn to investigate and recover."""
    ctx = RecordingContext()
    plugin.register(ctx)
    rec = json.loads(ctx.tools["assumption_record"]["handler"](
        {"statement": "the file missing.txt exists",
         "check": {"kind": "file_exists", "path": str(tmp_path / "missing.txt")}}
    ))
    json.loads(ctx.tools["assumption_check"]["handler"]({"id": rec["id"]}))  # INVALIDATED
    # No decision depends on it yet: nothing to warn about.
    assert plugin.on_pre_llm_call(session_id="s1") is None
    json.loads(ctx.tools["assumption_decide"]["handler"](
        {"statement": "consume missing.txt", "depends_on": [rec["id"]]}, session_id="s1"
    ))
    warned = plugin.on_pre_llm_call(session_id="s1")
    assert warned and "context" in warned
    assert "action" not in warned, "pre_llm_call must not block"
    assert "INVALIDATED" in warned["context"]


def test_pre_verify_holds_only_the_affected_session(plugin, tmp_path):
    """The hard gate is session-scoped: another session must be unaffected."""
    ctx = RecordingContext()
    plugin.register(ctx)
    rec = json.loads(ctx.tools["assumption_record"]["handler"](
        {"statement": "the file missing2.txt exists",
         "check": {"kind": "file_exists", "path": str(tmp_path / "missing2.txt")}}
    ))
    json.loads(ctx.tools["assumption_check"]["handler"]({"id": rec["id"]}))  # INVALIDATED
    json.loads(ctx.tools["assumption_decide"]["handler"](
        {"statement": "consume missing2.txt", "depends_on": [rec["id"]]}, session_id="s1"
    ))
    assert plugin.on_pre_verify(session_id="s1")["action"] == "continue"
    assert plugin.on_pre_verify(session_id="other") is None


def test_pre_verify_continues_only_when_blocked(plugin, tmp_path):
    ctx = RecordingContext()
    plugin.register(ctx)
    assert plugin.on_pre_verify(session_id="s1") is None
    rec = json.loads(ctx.tools["assumption_record"]["handler"](
        {"statement": "the file nope.txt exists",
         "check": {"kind": "file_exists", "path": str(tmp_path / "nope.txt")}}
    ))
    json.loads(ctx.tools["assumption_check"]["handler"]({"id": rec["id"]}))
    json.loads(ctx.tools["assumption_decide"]["handler"](
        {"statement": "use nope.txt", "depends_on": [rec["id"]]}, session_id="s1"
    ))
    out = plugin.on_pre_verify(session_id="s1")
    assert out and out["action"] == "continue"


def test_post_tool_call_never_persists_markers(plugin):
    """Untrusted tool output must not create a durable, re-injected assumption."""
    ctx = RecordingContext()
    plugin.register(ctx)
    plugin.on_post_tool_call(tool_name="terminal", result="ASSUMPTION: out/report.csv exists")
    plugin.on_post_tool_call(tool_name="fetch", result="Assumption: the deploy succeeded")
    summary = json.loads(ctx.tools["assumption_status"]["handler"]({}))
    assert summary["total"] == 0


def test_hooks_never_raise_on_hostile_input(plugin):
    ctx = RecordingContext()
    plugin.register(ctx)
    for hook in (plugin.on_pre_llm_call, plugin.on_pre_verify):
        hook(**{"unexpected": object(), "session_id": "s"})
    plugin.on_post_llm_call(assistant_response=None)
    plugin.on_post_tool_call(tool_name=None, result={"weird": object()})
    # A non-dict, non-str result must not crash the hook.
    plugin.on_post_tool_call(tool_name="x", result=object())


def test_slash_command_output(plugin, tmp_path):
    ctx = RecordingContext()
    plugin.register(ctx)
    handler = ctx.commands["assumptions"]["handler"]
    assert "nothing captured" in handler("list") or "no assumptions" in handler("list").lower()
    ctx.tools["assumption_record"]["handler"](
        {"statement": "the file c.txt exists", "check": {"kind": "file_exists", "path": str(tmp_path / "c.txt")}}
    )
    assert "Assumption Ledger" in handler("status")
    assert "Usage" in handler("bogus-subcommand-xyz")
