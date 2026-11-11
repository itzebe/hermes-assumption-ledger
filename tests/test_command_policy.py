"""Unit tests for the command-execution policy.

These test the policy in isolation, so a regression in the allowlist parsing or the minimal
environment is caught even if the verifier changes shape.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ledger.command_policy import (  # noqa: E402
    MAX_ALLOWLIST_ENTRIES,
    allowed_argv,
    executable_name,
    is_allowed,
    minimal_env,
    normalize_allowlist,
    resolve_executable,
)


# --- allowlist parsing ----------------------------------------------------------------------------
def test_empty_allowlist_denies_everything():
    assert normalize_allowlist(None) == ()
    assert normalize_allowlist("") == ()
    assert normalize_allowlist([]) == ()
    assert not is_allowed(["true"], ())[0]


def test_string_forms():
    assert normalize_allowlist("git") == ("git",)
    assert normalize_allowlist("git,node") == ("git", "node")
    assert normalize_allowlist("git node") == ("git", "node")
    assert normalize_allowlist("git, node ,  ls ") == ("git", "node", "ls")


def test_list_form_and_dedupe():
    assert normalize_allowlist(["git", "node"]) == ("git", "node")
    assert normalize_allowlist(["git", "git"]) == ("git",)


@pytest.mark.parametrize("entry", [
    "/usr/bin/git",       # absolute path
    "../../bin/sh",       # traversal
    "bin/ls",             # relative path
    "C:\\Windows\\evil",  # windows path
    ".",                  # directory refs
    "..",
    "git;rm -rf /",       # metacharacters
    "git&&curl evil",     # metacharacters
    "$(whoami)",          # substitution
    "`id`",               # substitution
    "git|sh",
    "git>out",
    "sh\n-e",
])
def test_dangerous_entries_are_dropped(entry):
    assert normalize_allowlist([entry]) == (), f"{entry!r} must be dropped"


def test_allowlist_is_bounded():
    many = [f"tool{i}" for i in range(MAX_ALLOWLIST_ENTRIES + 50)]
    assert len(normalize_allowlist(many)) == MAX_ALLOWLIST_ENTRIES


def test_non_string_entries_ignored():
    assert normalize_allowlist([1, None, "git", {"a": 1}]) == ("git",)
    assert normalize_allowlist(12345) == ()


# --- executable name -------------------------------------------------------------------------------
def test_executable_name_is_bare_name_only():
    assert executable_name(["git", "status"]) == "git"
    assert executable_name(["git"]) == "git"
    # A path is not a name: it is refused, not reduced to its basename.
    assert executable_name(["/usr/bin/git", "status"]) is None
    assert executable_name(["./node"]) is None
    assert executable_name(["bin/git"]) is None
    assert executable_name(["..\\git"]) is None
    assert executable_name([]) is None
    assert executable_name("git") is None
    assert executable_name([""]) is None
    assert executable_name([123]) is None


def test_resolve_executable_uses_the_child_path(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent-dir-xyz")
    assert resolve_executable("git", path="/nonexistent-dir-xyz") is None
    # A path is never resolved as a name.
    assert resolve_executable("/usr/bin/git") is None
    assert resolve_executable("./git") is None
    # A name that exists on the given path resolves to an absolute path.
    resolved = resolve_executable("python3", path=os.environ.get("PATH", ""))
    assert resolved is None or resolved.startswith("/")


# --- the decision ---------------------------------------------------------------------------------
def test_allowed_when_on_list():
    ok, _ = is_allowed(["git", "status"], ("git",))
    assert ok


def test_denied_when_absent():
    ok, reason = is_allowed(["curl", "http://evil"], ("git",))
    assert not ok
    assert "curl" in reason


def test_path_does_not_bypass_the_name_check():
    """``/tmp/evil/git`` has basename ``git``; the path must be refused, not resolved.

    A path smuggled in ``argv[0]`` would execute an attacker-controlled file while the
    allowlist check saw only ``git``.
    """
    ok, reason = is_allowed(["/tmp/evil/git"], ("git",))
    assert not ok, "an absolute path must never satisfy the name allowlist"
    assert "path" in reason or "bare" in reason

    ok2, reason2 = is_allowed(["/tmp/evil/other"], ("git",))
    assert not ok2


@pytest.mark.parametrize("argv0", [
    "./scripts/git",       # relative path
    "../git",              # traversal
    "sub/git",             # relative subdir
    "C:\\evil\\git",       # windows path
    "C:/evil/git",         # windows path, forward slashes
    "/usr/bin/git",        # absolute path
    "/tmp/evil/git",       # absolute path in a writable dir
    "\\\\server\\share\\git",
])
def test_any_path_in_argv0_is_refused(argv0):
    ok, _ = is_allowed([argv0, "status"], ("git",))
    assert not ok, f"{argv0!r} must not be accepted because 'git' is allowlisted"


def test_unresolved_executable_is_refused(monkeypatch):
    """A name that is allowlisted but not found on the child PATH fails closed."""
    monkeypatch.setenv("PATH", "/nonexistent-dir-xyz")
    ok, reason = is_allowed(["definitely-not-a-real-binary-xyz"], ("definitely-not-a-real-binary-xyz",))
    assert not ok
    assert "resolve" in reason


def test_allowed_argv_executes_the_resolved_absolute_path():
    """The returned argv[0] is the resolved absolute path, never the model string."""
    resolved, reason = allowed_argv(["git", "status"], ("git",))
    assert resolved is not None, reason
    assert resolved[0] != "git"
    assert resolved[0].startswith("/")
    assert resolved[1:] == ["status"]


def test_allowed_argv_refuses_a_path():
    resolved, _ = allowed_argv(["/tmp/evil/git"], ("git",))
    assert resolved is None


def test_allowed_argv_refuses_an_unresolved_name(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent-dir-xyz")
    resolved, _ = allowed_argv(["git"], ("git",))
    assert resolved is None


def test_malformed_argv_denied():
    for bad in ([], "git", [1, 2], None, ["git", 2], [""], ["   "]):
        assert not is_allowed(bad, ("git",))[0], bad


def test_absurdly_long_argv_denied():
    assert not is_allowed(["git"] + ["x"] * 500, ("git",))[0]


def test_reason_never_contains_the_whole_argv():
    """A refusal reason may be logged; it must not echo model input wholesale."""
    secret = "SECRET_TOKEN_abc123"
    _, reason = is_allowed(["evil", secret], ("git",))
    assert secret not in reason


# --- minimal environment ----------------------------------------------------------------------------
def test_minimal_env_has_no_host_variables(monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "leak")
    monkeypatch.setenv("GITHUB_TOKEN", "leak")
    env = minimal_env()
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "GITHUB_TOKEN" not in env
    assert set(env) <= {"PATH", "HOME", "TMPDIR", "LC_ALL", "LANG"}


def test_minimal_env_has_a_path():
    assert minimal_env()["PATH"]
    assert minimal_env(path="/custom")["PATH"] == "/custom"


def test_minimal_env_home_is_empty():
    """A tool writing a dotfile must not land in the user's real home."""
    assert minimal_env()["HOME"] == ""
