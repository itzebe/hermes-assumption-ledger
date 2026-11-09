"""Unit tests for the deterministic verifiers.

These test the *verifier* layer in isolation, including the exact provider-failure behaviour
that the lifecycle rules depend on: a timeout, a rate limit and a 5xx must all be ERROR.
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ledger.model import ERROR, FAIL, PASS  # noqa: E402
from ledger.verifiers import CheckContext, run_check  # noqa: E402


def _ctx(tmp_path, **kw):
    return CheckContext(root=tmp_path, cwd=tmp_path, **kw)


def _public_dns(hostname):
    """A resolver stub that answers every name with a public address, so tests never touch DNS."""
    return [(2, 1, 6, "", ("93.184.216.34", 0))]


# --- filesystem -------------------------------------------------------------------------------
def test_file_exists_pass_and_fail(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    assert run_check({"kind": "file_exists", "path": "a.txt"}, _ctx(tmp_path)).verdict == PASS
    assert run_check({"kind": "file_exists", "path": "b.txt"}, _ctx(tmp_path)).verdict == FAIL


def test_file_contains(tmp_path):
    (tmp_path / "c.txt").write_text("hello world")
    ok = run_check({"kind": "file_contains", "path": "c.txt", "pattern": "world"}, _ctx(tmp_path))
    assert ok.verdict == PASS
    no = run_check({"kind": "file_contains", "path": "c.txt", "pattern": "absent"}, _ctx(tmp_path))
    assert no.verdict == FAIL
    # Missing file is uncertainty, not a contradiction.
    miss = run_check({"kind": "file_contains", "path": "z.txt", "pattern": "x"}, _ctx(tmp_path))
    assert miss.verdict == ERROR


def test_file_hash(tmp_path):
    import hashlib

    p = tmp_path / "h.bin"
    p.write_bytes(b"payload")
    digest = hashlib.sha256(b"payload").hexdigest()
    assert run_check({"kind": "file_hash", "path": "h.bin", "sha256": digest}, _ctx(tmp_path)).verdict == PASS
    assert run_check({"kind": "file_hash", "path": "h.bin", "sha256": "0" * 64}, _ctx(tmp_path)).verdict == FAIL


# --- git --------------------------------------------------------------------------------------
@pytest.fixture()
def git_repo(tmp_path):
    def run(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    run("init", "-q")
    run("config", "user.email", "t@t.t")
    run("config", "user.name", "t")
    (tmp_path / "f.txt").write_text("1")
    run("add", ".")
    run("commit", "-qm", "first")
    return tmp_path


def test_git_branch_commit_clean(git_repo):
    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=git_repo, capture_output=True, text=True
    ).stdout.strip()
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=git_repo, capture_output=True, text=True
    ).stdout.strip()
    ctx = _ctx(git_repo)
    assert run_check({"kind": "git_branch", "branch": branch}, ctx).verdict == PASS
    assert run_check({"kind": "git_branch", "branch": "nope"}, ctx).verdict == FAIL
    assert run_check({"kind": "git_commit", "commit": head}, ctx).verdict == PASS
    assert run_check({"kind": "git_commit", "commit": "deadbeef"}, ctx).verdict == FAIL
    assert run_check({"kind": "git_clean"}, ctx).verdict == PASS
    # A modification to a tracked file is a real change to what was committed.
    (git_repo / "f.txt").write_text("2")
    assert run_check({"kind": "git_clean"}, ctx).verdict == FAIL

    # An untracked file (a build artifact, a log, the ledger's own database) is not a change to
    # what was committed. It is reported, but it must not invalidate the assumption by default.
    subprocess.run(["git", "checkout", "--", "f.txt"], cwd=git_repo, check=True, capture_output=True)
    (git_repo / "artifact.log").write_text("noise")
    clean = run_check({"kind": "git_clean"}, ctx)
    assert clean.verdict == PASS, clean.detail
    assert "artifact.log" in (clean.observed or {}).get("untracked", [])
    # ...unless the caller asks for untracked files to count.
    assert run_check({"kind": "git_clean", "include_untracked": True}, ctx).verdict == FAIL


def test_git_outside_repo_is_error(tmp_path):
    # Not a git repository: we cannot obtain evidence, so ERROR (never FAIL).
    assert run_check({"kind": "git_clean"}, _ctx(tmp_path)).verdict == ERROR


# --- env --------------------------------------------------------------------------------------
def test_env_set(tmp_path):
    ctx = _ctx(tmp_path, env={"PRESENT": "1"})
    assert run_check({"kind": "env_set", "name": "PRESENT"}, ctx).verdict == PASS
    assert run_check({"kind": "env_set", "name": "ABSENT"}, ctx).verdict == FAIL
    ctx2 = _ctx(tmp_path, env={"K": "a"})
    assert run_check({"kind": "env_set", "name": "K", "equals": "b"}, ctx2).verdict == FAIL


# --- command ----------------------------------------------------------------------------------
def test_command_disabled_by_default(tmp_path):
    """No allowlist means no execution at all, and the verdict is uncertainty, not failure."""
    ctx = _ctx(tmp_path)
    r = run_check({"kind": "command_succeeds", "argv": ["true"]}, ctx)
    assert r.verdict == ERROR
    assert "disabled" in r.detail


def test_command_allowlisted_runs(tmp_path):
    ctx = _ctx(tmp_path, command_allowlist=("true", "false"))
    assert run_check({"kind": "command_succeeds", "argv": ["true"]}, ctx).verdict == PASS
    assert run_check({"kind": "command_succeeds", "argv": ["false"]}, ctx).verdict == FAIL


def test_command_not_on_allowlist_is_error(tmp_path):
    """An unpermitted executable is a policy outcome (ERROR), never a contradiction."""
    ctx = _ctx(tmp_path, command_allowlist=("true",))
    r = run_check({"kind": "command_succeeds", "argv": ["rm", "-rf", "/"]}, ctx)
    assert r.verdict == ERROR
    assert "allowlist" in r.detail


def test_command_path_is_not_basename_matched(tmp_path):
    """A model-supplied path must not smuggle in an executable whose basename is allowed."""
    ctx = _ctx(tmp_path, command_allowlist=("git",))
    r = run_check({"kind": "command_succeeds", "argv": ["/tmp/evil/git", "status"]}, ctx)
    assert r.verdict == ERROR
    assert "path" in r.detail or "bare" in r.detail


def test_command_relative_path_is_refused(tmp_path):
    ctx = _ctx(tmp_path, command_allowlist=("git",))
    r = run_check({"kind": "command_succeeds", "argv": ["./scripts/git", "status"]}, ctx)
    assert r.verdict == ERROR


def test_command_runs_the_resolved_absolute_path(tmp_path, monkeypatch):
    """The executed program is the resolver's absolute path, not the model string."""
    seen = {}

    def runner(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, "", "")

    ctx = _ctx(tmp_path, command_allowlist=("true",), runner=runner)
    r = run_check({"kind": "command_succeeds", "argv": ["true"]}, ctx)
    assert r.verdict == PASS
    assert seen["argv"][0] != "true"
    assert seen["argv"][0].startswith("/")


def test_command_unresolved_binary_is_error(tmp_path, monkeypatch):
    """An allowlisted name that is not on the child PATH fails closed."""
    monkeypatch.setenv("PATH", "/nonexistent-dir-xyz")
    ctx = _ctx(tmp_path, command_allowlist=("git",))
    r = run_check({"kind": "command_succeeds", "argv": ["git", "status"]}, ctx)
    assert r.verdict == ERROR


def test_command_allowlist_is_not_widened_by_spec(tmp_path):
    """A check spec cannot grant itself permission."""
    ctx = _ctx(tmp_path, command_allowlist=("true",))
    r = run_check(
        {"kind": "command_succeeds", "argv": ["false"], "command_allowlist": ["false"]}, ctx
    )
    assert r.verdict == ERROR


def test_command_missing_binary_is_error(tmp_path):
    ctx = _ctx(tmp_path, command_allowlist=("definitely-not-a-real-binary-xyz",))
    r = run_check({"kind": "command_succeeds", "argv": ["definitely-not-a-real-binary-xyz"]}, ctx)
    assert r.verdict == ERROR


def test_command_child_environment_is_minimal(tmp_path, monkeypatch):
    """The child must not inherit the host environment."""
    monkeypatch.setenv("LEDGER_SECRET_CANARY", "leaked")
    script = tmp_path / "env.py"
    script.write_text("import os, sys; sys.exit(0 if 'LEDGER_SECRET_CANARY' not in os.environ else 3)")
    ctx = _ctx(tmp_path, command_allowlist=("python3",))
    r = run_check(
        {"kind": "command_succeeds", "argv": ["python3", str(script)]}, ctx
    )
    assert r.verdict == PASS, r.detail


def test_command_shell_metacharacters_are_literal(tmp_path):
    """argv is passed with shell=False, so a metacharacter is an argument, not a command."""
    marker = tmp_path / "pwned.txt"
    ctx = _ctx(tmp_path, command_allowlist=("true",))
    r = run_check(
        {"kind": "command_succeeds", "argv": ["true", ";", f"touch {marker}"]}, ctx
    )
    assert r.verdict == PASS
    assert not marker.exists(), "shell interpolation must not occur"


# --- http -------------------------------------------------------------------------------------
def test_http_status_ok(tmp_path):
    ctx = _ctx(tmp_path, http_get=lambda u, t: {"status": 200, "body": "ok"}, url_resolver=_public_dns)
    assert run_check({"kind": "http_status", "url": "https://example.com", "expected_status": 200}, ctx).verdict == PASS
    assert run_check({"kind": "http_status", "url": "https://example.com", "expected_status": 204}, ctx).verdict == FAIL


def test_http_5xx_is_error_not_fail(tmp_path):
    ctx = _ctx(tmp_path, http_get=lambda u, t: {"status": 500, "body": ""}, url_resolver=_public_dns)
    assert run_check({"kind": "http_status", "url": "https://example.com"}, ctx).verdict == ERROR


def test_http_429_is_error(tmp_path):
    ctx = _ctx(tmp_path, http_get=lambda u, t: {"status": 429, "body": ""}, url_resolver=_public_dns)
    assert run_check({"kind": "http_status", "url": "https://example.com"}, ctx).verdict == ERROR


def test_http_404_is_fail(tmp_path):
    ctx = _ctx(tmp_path, http_get=lambda u, t: {"status": 404, "body": ""}, url_resolver=_public_dns)
    assert run_check({"kind": "http_status", "url": "https://example.com"}, ctx).verdict == FAIL


def test_http_transport_error_is_error(tmp_path):
    ctx = _ctx(tmp_path, http_get=lambda u, t: {"error": "dns failure", "transient": True}, url_resolver=_public_dns)
    assert run_check({"kind": "http_status", "url": "https://example.com"}, ctx).verdict == ERROR


def test_http_getter_raising_is_error(tmp_path):
    def boom(u, t):
        raise OSError("socket closed")

    ctx = _ctx(tmp_path, http_get=boom, url_resolver=_public_dns)
    assert run_check({"kind": "http_status", "url": "https://example.com"}, ctx).verdict == ERROR


def test_http_json_field(tmp_path):
    body = '{"version": "1.2.3", "nested": {"ok": true}, "list": [10, 20]}'
    ctx = _ctx(tmp_path, http_get=lambda u, t: {"status": 200, "body": body}, url_resolver=_public_dns)
    assert run_check({"kind": "http_json_field", "url": "https://example.com", "pointer": "version", "equals": "1.2.3"}, ctx).verdict == PASS
    assert run_check({"kind": "http_json_field", "url": "https://example.com", "pointer": "version", "equals": "9.9.9"}, ctx).verdict == FAIL
    assert run_check({"kind": "http_json_field", "url": "https://example.com", "pointer": "nested.ok"}, ctx).verdict == PASS
    assert run_check({"kind": "http_json_field", "url": "https://example.com", "pointer": "list.1", "equals": 20}, ctx).verdict == PASS
    assert run_check({"kind": "http_json_field", "url": "https://example.com", "pointer": "missing"}, ctx).verdict == FAIL
    # Non-JSON body is uncertainty.
    ctx2 = _ctx(tmp_path, http_get=lambda u, t: {"status": 200, "body": "<html>"}, url_resolver=_public_dns)
    assert run_check({"kind": "http_json_field", "url": "https://example.com", "pointer": "a"}, ctx2).verdict == ERROR


# --- database ---------------------------------------------------------------------------------
@pytest.fixture()
def sqlite_db(tmp_path):
    db = tmp_path / "app.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
    conn.executemany("INSERT INTO users (name) VALUES (?)", [("a",), ("b",), ("c",)])
    conn.commit()
    conn.close()
    return db


def test_db_table_exists(sqlite_db, tmp_path):
    ctx = _ctx(tmp_path)
    assert run_check({"kind": "db_table_exists", "path": "app.db", "table": "users"}, ctx).verdict == PASS
    assert run_check({"kind": "db_table_exists", "path": "app.db", "table": "nope"}, ctx).verdict == FAIL


def test_db_row_count(sqlite_db, tmp_path):
    ctx = _ctx(tmp_path)
    assert run_check({"kind": "db_row_count", "path": "app.db", "table": "users", "equals": 3}, ctx).verdict == PASS
    assert run_check({"kind": "db_row_count", "path": "app.db", "table": "users", "equals": 99}, ctx).verdict == FAIL
    assert run_check({"kind": "db_row_count", "path": "app.db", "table": "users", "min": 1}, ctx).verdict == PASS
    assert run_check({"kind": "db_row_count", "path": "app.db", "table": "users", "max": 2}, ctx).verdict == FAIL


def test_db_unsafe_table_name_is_error(sqlite_db, tmp_path):
    ctx = _ctx(tmp_path)
    r = run_check({"kind": "db_row_count", "path": "app.db", "table": "users; DROP TABLE users"}, ctx)
    assert r.verdict == ERROR


def test_db_missing_file_is_error(tmp_path):
    ctx = _ctx(tmp_path)
    assert run_check({"kind": "db_table_exists", "path": "nope.db", "table": "t"}, ctx).verdict == ERROR


# --- dispatch ---------------------------------------------------------------------------------
def test_unknown_kind_is_error(tmp_path):
    assert run_check({"kind": "not_a_real_kind"}, _ctx(tmp_path)).verdict == ERROR


def test_verifier_registry_is_closed(tmp_path):
    from ledger.verifiers import SUPPORTED_KINDS

    assert "file_exists" in SUPPORTED_KINDS
    assert "db_row_count" in SUPPORTED_KINDS
    assert len(SUPPORTED_KINDS) == 12  # unchanged: hardening, not removal
