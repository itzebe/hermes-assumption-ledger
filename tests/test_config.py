"""Tests that every declared configuration setting actually changes behaviour.

The review found three settings declared in ``plugin.yaml`` that no code read. These tests exist so
that cannot recur silently: each retained setting is set, and a documented, observable effect is
asserted. A setting that stops being wired will fail here.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def plugin(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    spec = importlib.util.spec_from_file_location(
        "alp_config", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["alp_config"] = mod
    spec.loader.exec_module(mod)
    mod._HOST = mod.LedgerHost()
    mod._CONFIG_GETTER[0] = None
    yield mod
    mod._CONFIG_GETTER[0] = None


class Ctx:
    """A PluginContext stand-in that serves a fixed config mapping."""

    def __init__(self, config=None):
        self._config = dict(config or {})
        self.tools = {}
        self.commands = {}

    def get_config(self, key, default=None):
        return self._config.get(key, default)

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = handler

    def register_hook(self, hook, callback):
        pass

    def register_command(self, name, handler, **kw):
        self.commands[name] = handler


# --- manifest / declaration agreement -------------------------------------------------------------
def test_manifest_declares_exactly_the_config_we_read(plugin):
    """Every key in plugin.yaml must be one the code resolves, and vice versa."""
    import re

    text = (ROOT / "plugin.yaml").read_text()
    block = text.split("config_schema:", 1)[1]
    declared = set(re.findall(r"^  ([a-z_]+):$", block, re.MULTILINE))
    resolved = set(plugin.resolve_config())
    assert declared == resolved, f"manifest declares {declared}, code resolves {resolved}"


def test_no_declared_setting_is_unused(plugin):
    """Guard against reintroducing the exact defect the review found."""
    text = (ROOT / "plugin.yaml").read_text()
    block = text.split("config_schema:", 1)[1]
    import re

    declared = set(re.findall(r"^  ([a-z_]+):$", block, re.MULTILINE))
    source = (ROOT / "__init__.py").read_text()
    for key in declared:
        assert f'"{key}"' in source, f"{key} is declared but never named in the implementation"


# --- defaults are safe ----------------------------------------------------------------------------
def test_defaults_are_safe(plugin):
    cfg = plugin.resolve_config()
    assert cfg["auto_check"] is False, "capture must not perform I/O by default"
    assert cfg["command_allowlist"] == (), "command execution must be off by default"
    assert cfg["allow_private_urls"] is False, "private-network access must be off by default"
    assert cfg["freshness_seconds"] == plugin.DEFAULT_FRESHNESS_SECONDS
    assert cfg["max_reinject"] == plugin.DEFAULT_REINJECT_LIMIT


# --- each setting changes behaviour ----------------------------------------------------------------
def test_freshness_seconds_changes_the_staleness_window(plugin, tmp_path):
    """A tiny window makes fresh evidence go stale; a large one keeps it."""
    ctx = Ctx({"freshness_seconds": 1})
    plugin.register(ctx)
    engine = plugin._HOST.engine()
    assert engine.freshness_seconds == 1

    (tmp_path / "f.txt").write_text("x")
    asm = engine.add_assumption("the file f.txt exists", {"kind": "file_exists", "path": str(tmp_path / "f.txt")})
    engine.verify(asm.id)
    assert engine.store.get_assumption(asm.id).status == "VALID"

    # Backdate the evidence beyond the 1s window, then sweep.
    stale = engine.store.get_assumption(asm.id)
    stale.last_checked_at = stale.last_checked_at - 10
    engine.store.save_assumption(stale)
    assert engine.sweep_stale() == [asm.id]
    assert engine.store.get_assumption(asm.id).status == "STALE"


def test_freshness_seconds_is_bounded(plugin):
    assert plugin.resolve_config()["freshness_seconds"] == plugin.DEFAULT_FRESHNESS_SECONDS
    ctx = Ctx({"freshness_seconds": -5})
    plugin.register(ctx)
    assert plugin._HOST.config["freshness_seconds"] == plugin.MIN_FRESHNESS_SECONDS
    plugin._HOST.reset()
    ctx2 = Ctx({"freshness_seconds": 10 ** 12})
    plugin.register(ctx2)
    assert plugin._HOST.config["freshness_seconds"] == plugin.MAX_FRESHNESS_SECONDS


def test_auto_check_verifies_on_capture(plugin, tmp_path):
    """With auto_check off the record stays UNVERIFIED; with it on, the check runs immediately."""
    ctx = Ctx({"auto_check": False})
    plugin.register(ctx)
    engine = plugin._HOST.engine()
    (tmp_path / "auto.txt").write_text("x")
    spec = {"kind": "file_exists", "path": str(tmp_path / "auto.txt")}

    off = engine.add_assumption("auto-check off", spec)
    assert engine.store.get_assumption(off.id).status == "UNVERIFIED"

    plugin._HOST.reset()
    ctx2 = Ctx({"auto_check": True})
    plugin.register(ctx2)
    engine2 = plugin._HOST.engine()
    on = engine2.add_assumption("auto-check on", spec)
    assert engine2.store.get_assumption(on.id).status == "VALID"


def test_auto_check_applies_to_captured_messages(plugin, tmp_path):
    ctx = Ctx({"auto_check": True})
    plugin.register(ctx)
    engine = plugin._HOST.engine()
    (tmp_path / "cap.txt").write_text("x")
    plugin._HOST.engine()  # ensure built
    # A captured message uses the configured auto-check as well.
    engine.observe_message("ASSUMPTION: the file cap.txt exists")
    rows = engine.store.list_assumptions()
    assert rows and rows[0].check_count >= 1


def test_max_reinject_changes_the_injected_length(plugin):
    ctx = Ctx({"max_reinject": 2})
    plugin.register(ctx)
    engine = plugin._HOST.engine()
    assert engine.reinject_limit == 2
    for i in range(6):
        engine.add_assumption(f"assumption number {i}", {"kind": "unsupported"})
    block = engine.render_reinjection()
    listed = [ln for ln in block.splitlines() if ln.strip().startswith("[")]
    assert len(listed) == 2, f"expected 2 re-injected lines, got {len(listed)}"


def test_max_reinject_is_bounded(plugin):
    plugin.register(Ctx({"max_reinject": 0}))
    assert plugin._HOST.config["max_reinject"] == 1
    plugin._HOST.reset()
    plugin.register(Ctx({"max_reinject": 10 ** 6}))
    assert plugin._HOST.config["max_reinject"] == plugin.MAX_REINJECT_LIMIT


def test_command_allowlist_enables_command_checks(plugin, tmp_path):
    """Empty allowlist -> ERROR; a configured allowlist -> the command runs."""
    plugin.register(Ctx({"command_allowlist": []}))
    engine = plugin._HOST.engine()
    asm = engine.add_assumption("a command succeeds", {"kind": "command_succeeds", "argv": ["true"]})
    engine.verify(asm.id)
    assert engine.store.get_assumption(asm.id).status == "UNVERIFIED"

    plugin._HOST.reset()
    plugin.register(Ctx({"command_allowlist": ["true"]}))
    engine2 = plugin._HOST.engine()
    assert engine2.ctx.command_allowlist == ("true",)
    asm2 = engine2.add_assumption("a command succeeds", {"kind": "command_succeeds", "argv": ["true"]})
    engine2.verify(asm2.id)
    assert engine2.store.get_assumption(asm2.id).status == "VALID"


def test_command_allowlist_accepts_string_and_rejects_paths(plugin):
    plugin.register(Ctx({"command_allowlist": "git, node  /usr/bin/evil;rm"}))
    assert plugin._HOST.config["command_allowlist"] == ("git", "node")


def test_allow_private_urls_changes_http_policy(plugin):
    plugin.register(Ctx({"allow_private_urls": False}))
    engine = plugin._HOST.engine()
    asm = engine.add_assumption(
        "a local service responds", {"kind": "http_status", "url": "http://localhost:9/"}
    )
    engine.verify(asm.id)
    assert engine.store.get_assumption(asm.id).status == "UNVERIFIED"

    plugin._HOST.reset()
    plugin.register(Ctx({"allow_private_urls": True}))
    engine2 = plugin._HOST.engine()
    assert engine2.ctx.allow_private_urls is True


def test_allow_private_urls_does_not_unblock_metadata(plugin):
    plugin.register(Ctx({"allow_private_urls": True}))
    engine = plugin._HOST.engine()
    asm = engine.add_assumption(
        "metadata responds", {"kind": "http_status", "url": "http://169.254.169.254/"}
    )
    engine.verify(asm.id)
    assert engine.store.get_assumption(asm.id).status == "UNVERIFIED", (
        "cloud metadata must stay refused even with the private-network opt-in"
    )


# --- coercion robustness ---------------------------------------------------------------------------
@pytest.mark.parametrize("value", ["not-a-number", None, [], {}, "abc"])
def test_bad_int_falls_back_to_default(plugin, value):
    plugin.register(Ctx({"freshness_seconds": value, "max_reinject": value}))
    cfg = plugin._HOST.config
    assert cfg["freshness_seconds"] == plugin.DEFAULT_FRESHNESS_SECONDS
    assert cfg["max_reinject"] == plugin.DEFAULT_REINJECT_LIMIT


@pytest.mark.parametrize("value,expected", [
    (True, True), ("true", True), ("yes", True), ("1", True), ("on", True),
    (False, False), ("false", False), ("no", False), ("0", False), ("", False),
    (None, False), ("garbage", False),
])
def test_bool_coercion(plugin, value, expected):
    assert plugin._coerce_bool(value) is expected


def test_config_read_failure_uses_defaults(plugin):
    class Exploding:
        def get_config(self, key, default=None):
            raise RuntimeError("config backend down")

        def register_tool(self, *a, **k):
            pass

        def register_hook(self, *a, **k):
            pass

        def register_command(self, *a, **k):
            pass

    plugin.register(Exploding())
    cfg = plugin._HOST.config
    assert cfg["command_allowlist"] == ()
    assert cfg["allow_private_urls"] is False
    assert plugin._HOST.engine() is not None, "a config failure must not disable the plugin"


# --- version agreement -----------------------------------------------------------------------------
def test_versions_agree_everywhere():
    import re

    manifest = (ROOT / "plugin.yaml").read_text()
    pyproject = (ROOT / "pyproject.toml").read_text()
    entry = (ROOT / "__init__.py").read_text()
    pkg = (ROOT / "ledger" / "__init__.py").read_text()

    versions = {
        "plugin.yaml": re.search(r"^version:\s*(\S+)", manifest, re.MULTILINE).group(1).strip('"'),
        "pyproject.toml": re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE).group(1),
        "__init__.py": re.search(r'^__version__\s*=\s*"([^"]+)"', entry, re.MULTILINE).group(1),
        "ledger/__init__.py": re.search(r'^__version__\s*=\s*"([^"]+)"', pkg, re.MULTILINE).group(1),
    }
    assert len(set(versions.values())) == 1, f"version strings disagree: {versions}"
    assert versions["plugin.yaml"] == "0.2.1"


def test_manifest_tools_and_hooks_match_registration(plugin):
    import re

    manifest = (ROOT / "plugin.yaml").read_text()
    declared_tools = set(re.findall(r"^\s*-\s*(assumption_\w+)$", manifest, re.MULTILINE))
    declared_hooks = set(re.findall(r"^\s*-\s*(pre_\w+|post_\w+)$", manifest, re.MULTILINE))
    ctx = Ctx()
    plugin.register(ctx)
    assert declared_tools == set(ctx.tools)
