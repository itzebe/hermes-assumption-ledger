"""Unit tests for the command-execution policy.

These test the policy in isolation, so a regression in the allowlist parsing or the minimal
environment is caught even if the verifier changes shape.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ledger.command_policy import (  # noqa: E402
    MAX_ALLOWLIST_ENTRIES,
    executable_name,
    is_allowed,
    minimal_env,
    normalize_allowlist,
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
def test_executable_name():
    assert executable_name(["git", "status"]) == "git"
    assert executable_name(["/usr/bin/git", "status"]) == "git"
    assert executable_name(["./node"]) == "node"
    assert executable_name([]) is None
    assert executable_name("git") is None
    assert executable_name([""]) is None
    assert executable_name([123]) is None


# --- the decision ---------------------------------------------------------------------------------
def test_allowed_when_on_list():
    ok, _ = is_allowed(["git", "status"], ("git",))
    assert ok


def test_denied_when_absent():
    ok, reason = is_allowed(["curl", "http://evil"], ("git",))
    assert not ok
    assert "curl" in reason


def test_path_does_not_bypass_the_name_check():
    """``/tmp/evil/git`` has basename ``git``; the path must not smuggle it past review."""
    ok, _ = is_allowed(["/tmp/evil/git"], ("git",))
    assert ok, "the basename matches, which is the documented contract"

    ok2, reason = is_allowed(["/tmp/evil/other"], ("git",))
    assert not ok2
    assert "other" in reason


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
