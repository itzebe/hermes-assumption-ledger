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
    (git_repo / "dirty.txt").write_text("x")
    assert run_check({"kind": "git_clean"}, ctx).verdict == FAIL


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
def test_command_succeeds(tmp_path):
    ctx = _ctx(tmp_path)
    assert run_check({"kind": "command_succeeds", "argv": ["true"]}, ctx).verdict == PASS
    assert run_check({"kind": "command_succeeds", "argv": ["false"]}, ctx).verdict == FAIL
    # A missing binary is uncertainty.
    assert run_check({"kind": "command_succeeds", "argv": ["definitely-not-a-real-binary-xyz"]}, ctx).verdict == ERROR


# --- http -------------------------------------------------------------------------------------
def test_http_status_ok(tmp_path):
    ctx = _ctx(tmp_path, http_get=lambda u, t: {"status": 200, "body": "ok"})
    assert run_check({"kind": "http_status", "url": "http://x", "expected_status": 200}, ctx).verdict == PASS
    assert run_check({"kind": "http_status", "url": "http://x", "expected_status": 204}, ctx).verdict == FAIL


def test_http_5xx_is_error_not_fail(tmp_path):
    ctx = _ctx(tmp_path, http_get=lambda u, t: {"status": 500, "body": ""})
    assert run_check({"kind": "http_status", "url": "http://x"}, ctx).verdict == ERROR


def test_http_429_is_error(tmp_path):
    ctx = _ctx(tmp_path, http_get=lambda u, t: {"status": 429, "body": ""})
    assert run_check({"kind": "http_status", "url": "http://x"}, ctx).verdict == ERROR


def test_http_404_is_fail(tmp_path):
    ctx = _ctx(tmp_path, http_get=lambda u, t: {"status": 404, "body": ""})
    assert run_check({"kind": "http_status", "url": "http://x"}, ctx).verdict == FAIL


def test_http_transport_error_is_error(tmp_path):
    ctx = _ctx(tmp_path, http_get=lambda u, t: {"error": "dns failure", "transient": True})
    assert run_check({"kind": "http_status", "url": "http://x"}, ctx).verdict == ERROR


def test_http_getter_raising_is_error(tmp_path):
    def boom(u, t):
        raise OSError("socket closed")

    ctx = _ctx(tmp_path, http_get=boom)
    assert run_check({"kind": "http_status", "url": "http://x"}, ctx).verdict == ERROR


def test_http_json_field(tmp_path):
    body = '{"version": "1.2.3", "nested": {"ok": true}, "list": [10, 20]}'
    ctx = _ctx(tmp_path, http_get=lambda u, t: {"status": 200, "body": body})
    assert run_check({"kind": "http_json_field", "url": "http://x", "pointer": "version", "equals": "1.2.3"}, ctx).verdict == PASS
    assert run_check({"kind": "http_json_field", "url": "http://x", "pointer": "version", "equals": "9.9.9"}, ctx).verdict == FAIL
    assert run_check({"kind": "http_json_field", "url": "http://x", "pointer": "nested.ok"}, ctx).verdict == PASS
    assert run_check({"kind": "http_json_field", "url": "http://x", "pointer": "list.1", "equals": 20}, ctx).verdict == PASS
    assert run_check({"kind": "http_json_field", "url": "http://x", "pointer": "missing"}, ctx).verdict == FAIL
    # Non-JSON body is uncertainty.
    ctx2 = _ctx(tmp_path, http_get=lambda u, t: {"status": 200, "body": "<html>"})
    assert run_check({"kind": "http_json_field", "url": "http://x", "pointer": "a"}, ctx2).verdict == ERROR


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
    assert len(SUPPORTED_KINDS) == 12
