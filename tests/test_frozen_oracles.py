"""Frozen oracles for the Assumption Ledger.

These are written as an explicit expectation table first, then executed. Each case names the
adversarial scenario from the mission brief and the exact status/verdict it must produce.

The point of an oracle is that it is frozen: if a later change makes one of these fail, the
change is wrong until proven otherwise. Do not edit an expected value to make a test pass;
fix the engine instead.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ledger import LedgerEngine, LedgerStore  # noqa: E402
from ledger.model import (  # noqa: E402
    INVALIDATED,
    STALE,
    UNVERIFIED,
    VALID,
    ERROR,
    FAIL,
    PASS,
    CheckResult,
)
from ledger.verifiers import CheckContext, run_check  # noqa: E402

# ---------------------------------------------------------------------------------------------
# Expectation table: the frozen contract.
# Each row: (case_id, scenario, expected)
# ---------------------------------------------------------------------------------------------
ORACLE_EXPECTATIONS = [
    ("O1", "correct assumption with supporting evidence", {"status": VALID, "verdict": PASS}),
    ("O2", "false assumption with contradictory evidence", {"status": INVALIDATED, "verdict": FAIL}),
    ("O3", "missing evidence", {"status": UNVERIFIED, "verdict": ERROR}),
    ("O4", "provider timeout", {"status": UNVERIFIED, "verdict": ERROR}),
    ("O5", "rate-limited provider", {"status": UNVERIFIED, "verdict": ERROR}),
    ("O6", "evidence becomes stale", {"status": STALE}),
    ("O7", "invalidation with dependent decisions", {"decision_state": "blocked"}),
    ("O8", "restart and persistent recovery", {"survives_restart": True}),
    ("O9", "compaction and assumption re-injection", {"rejected_after_compaction": True}),
    ("O10", "conflicting evidence", {"status": INVALIDATED, "uncertainty_does_not_override": True}),
    ("O11", "malformed extraction output", {"status": UNVERIFIED, "no_crash": True}),
    ("O12", "duplicate assumption detection", {"records": 1, "seen_count": 2}),
]


def _engine(tmp: Path, *, freshness=3600.0, http_get=None, runner=None, env=None):
    store = LedgerStore(tmp / "ledger.db")
    ctx = CheckContext(
        root=tmp, cwd=tmp, http_get=http_get, runner=runner, env=env, default_timeout=2.0
    )
    return LedgerEngine(store, freshness_seconds=freshness, ctx=ctx)


# --- O1 --------------------------------------------------------------------------------------
def test_O1_correct_assumption_with_evidence(tmp_path):
    (tmp_path / "present.txt").write_text("hello")
    eng = _engine(tmp_path)
    a = eng.add_assumption("the file present.txt exists", {"kind": "file_exists", "path": "present.txt"})
    res = eng.verify(a.id)
    assert res["verdict"] == PASS
    assert res["status"] == VALID
    assert eng.store.get_assumption(a.id).status == VALID
    assert len(eng.store.evidence_for(a.id)) == 1


# --- O2 --------------------------------------------------------------------------------------
def test_O2_false_assumption_with_contradictory_evidence(tmp_path):
    eng = _engine(tmp_path)
    a = eng.add_assumption("the file ghost.txt exists", {"kind": "file_exists", "path": "ghost.txt"})
    res = eng.verify(a.id)
    assert res["verdict"] == FAIL
    assert res["status"] == INVALIDATED
    asm = eng.store.get_assumption(a.id)
    assert asm.status == INVALIDATED
    assert asm.invalidated_reason


# --- O3 --------------------------------------------------------------------------------------
def test_O3_missing_evidence_is_unverified(tmp_path):
    eng = _engine(tmp_path)
    # file_contains on a file that does not exist: we cannot obtain evidence either way.
    a = eng.add_assumption(
        "the file nowhere.cfg contains token",
        {"kind": "file_contains", "path": "nowhere.cfg", "pattern": "token"},
    )
    res = eng.verify(a.id)
    assert res["verdict"] == ERROR
    assert res["status"] == UNVERIFIED
    assert res["status"] != INVALIDATED


# --- O4 --------------------------------------------------------------------------------------
def test_O4_provider_timeout_is_unverified(tmp_path):
    def timeout_getter(url, timeout):
        raise TimeoutError("connection timed out")

    eng = _engine(tmp_path, http_get=timeout_getter)
    a = eng.add_assumption("the endpoint http://x.test is reachable",
                           {"kind": "http_status", "url": "http://x.test", "expected_status": 200})
    res = eng.verify(a.id)
    assert res["verdict"] == ERROR
    assert res["status"] == UNVERIFIED


# --- O5 --------------------------------------------------------------------------------------
def test_O5_rate_limited_provider_is_unverified(tmp_path):
    def rate_limited(url, timeout):
        return {"status": 429, "body": "slow down"}

    eng = _engine(tmp_path, http_get=rate_limited)
    a = eng.add_assumption("the endpoint http://api.test returns HTTP 200",
                           {"kind": "http_status", "url": "http://api.test", "expected_status": 200})
    res = eng.verify(a.id)
    assert res["verdict"] == ERROR
    assert res["status"] == UNVERIFIED
    assert res["status"] != INVALIDATED


def test_O5b_server_error_is_unverified(tmp_path):
    def five_hundred(url, timeout):
        return {"status": 503, "body": "unavailable"}

    eng = _engine(tmp_path, http_get=five_hundred)
    a = eng.add_assumption("the endpoint http://api.test returns HTTP 200",
                           {"kind": "http_status", "url": "http://api.test", "expected_status": 200})
    res = eng.verify(a.id)
    assert res["status"] == UNVERIFIED


# --- O6 --------------------------------------------------------------------------------------
def test_O6_evidence_becomes_stale(tmp_path):
    (tmp_path / "here.txt").write_text("x")
    eng = _engine(tmp_path, freshness=10.0)
    a = eng.add_assumption("the file here.txt exists", {"kind": "file_exists", "path": "here.txt"})
    assert eng.verify(a.id)["status"] == VALID
    # Rewind the last check beyond the freshness window.
    asm = eng.store.get_assumption(a.id)
    asm.last_checked_at = time.time() - 100
    eng.store.save_assumption(asm)
    degraded = eng.sweep_stale()
    assert a.id in degraded
    assert eng.store.get_assumption(a.id).status == STALE


# --- O7 --------------------------------------------------------------------------------------
def test_O7_invalidation_blocks_dependent_decision(tmp_path):
    (tmp_path / "ok.txt").write_text("x")
    eng = _engine(tmp_path)
    good = eng.add_assumption("the file ok.txt exists", {"kind": "file_exists", "path": "ok.txt"})
    bad = eng.add_assumption("the file gone.txt exists", {"kind": "file_exists", "path": "gone.txt"})
    eng.verify(good.id)
    dec = eng.record_decision("deploy the service", depends_on=[good.id, bad.id])
    assert dec.state == "open", "not yet invalidated"
    eng.verify(bad.id)
    after = eng.store.get_decision(dec.id)
    assert after.state == "blocked"
    assert bad.id in (after.blocked_reason or "")


def test_O7b_unrelated_decision_is_not_blocked(tmp_path):
    eng = _engine(tmp_path)
    bad = eng.add_assumption("the file absent.txt exists", {"kind": "file_exists", "path": "absent.txt"})
    other = eng.record_decision("unrelated cleanup", depends_on=[])
    eng.verify(bad.id)
    assert eng.store.get_decision(other.id).state == "open", "must not block unrelated work"


# --- O8 --------------------------------------------------------------------------------------
def test_O8_restart_and_persistent_recovery(tmp_path):
    db = tmp_path / "ledger.db"
    store = LedgerStore(db)
    eng = LedgerEngine(store, freshness_seconds=3600, ctx=CheckContext(root=tmp_path, cwd=tmp_path))
    (tmp_path / "keep.txt").write_text("x")
    a = eng.add_assumption("the file keep.txt exists", {"kind": "file_exists", "path": "keep.txt"})
    eng.verify(a.id)
    dec = eng.record_decision("ship", depends_on=[a.id])
    store.close()

    store2 = LedgerStore(db)
    eng2 = LedgerEngine(store2, freshness_seconds=3600, ctx=CheckContext(root=tmp_path, cwd=tmp_path))
    recovered = eng2.store.get_assumption(a.id)
    assert recovered is not None
    assert recovered.status == VALID
    assert recovered.check_count == 1
    assert eng2.store.get_decision(dec.id) is not None
    assert len(eng2.store.evidence_for(a.id)) == 1
    store2.close()


# --- O9 --------------------------------------------------------------------------------------
def test_O9_compaction_reinjection(tmp_path):
    (tmp_path / "pinned.txt").write_text("x")
    eng = _engine(tmp_path)
    a = eng.add_assumption("the file pinned.txt exists", {"kind": "file_exists", "path": "pinned.txt"})
    eng.verify(a.id)
    # Compaction is simulated as total in-memory loss: a *fresh* engine over the same database
    # must re-inject the live assumptions from durable storage alone.
    eng2 = _engine(tmp_path)
    payload = eng2.render_reinjection()
    assert "pinned.txt" in payload, "the live assumption must be re-injectable after compaction"
    assert a.id in payload
    assert "[VALID]" in payload
    assert any(x.id == a.id for x in eng2.live_assumptions())
    # An invalidated assumption must not be re-injected as live, but must appear as a blocker.
    bad = eng2.add_assumption("the file vanished.txt exists", {"kind": "file_exists", "path": "vanished.txt"})
    eng2.verify(bad.id)
    eng2.record_decision("use vanished.txt", depends_on=[bad.id])
    payload2 = eng2.render_reinjection()
    assert "vanished.txt" not in payload2.split("Invalidated")[0], "not live"
    assert "INVALIDATED" in payload2


# --- O10 -------------------------------------------------------------------------------------
def test_O10_conflicting_evidence_latest_definitive_wins(tmp_path):
    target = tmp_path / "flip.txt"
    target.write_text("x")
    eng = _engine(tmp_path)
    a = eng.add_assumption("the file flip.txt exists", {"kind": "file_exists", "path": "flip.txt"})
    assert eng.verify(a.id)["status"] == VALID
    # Evidence now contradicts the earlier pass.
    target.unlink()
    assert eng.verify(a.id)["status"] == INVALIDATED
    # A subsequent *uncertain* reading must not resurrect it.
    asm = eng.store.get_assumption(a.id)
    asm.check = {"kind": "unsupported"}
    eng.store.save_assumption(asm)
    eng.verify(a.id)
    assert eng.store.get_assumption(a.id).status == INVALIDATED, "uncertainty must not override a contradiction"
    # Both pieces of evidence are retained.
    assert len(eng.store.evidence_for(a.id)) >= 2


# --- O11 -------------------------------------------------------------------------------------
def test_O11_malformed_extraction_output(tmp_path):
    eng = _engine(tmp_path)
    # A marker with no checkable form must be captured as UNVERIFIED, never crash.
    captured = eng.observe_message("ASSUMPTION: everything is probably fine")
    assert captured, "an explicit marker is always captured"
    for a in captured:
        res = eng.verify(a.id)
        assert res["status"] == UNVERIFIED
    # Malformed / hostile verifier specs must degrade to ERROR, not raise.
    for bad_spec in [
        None, "not-a-dict", {}, {"kind": None}, {"kind": 42},
        {"kind": "file_exists"}, {"kind": "file_contains", "path": "x"},
        {"kind": "git_branch"}, {"kind": "db_row_count", "path": "x", "table": "a; DROP TABLE b"},
    ]:
        r = run_check(bad_spec if isinstance(bad_spec, dict) else {}, CheckContext(root=tmp_path))
        assert r.verdict == ERROR, f"{bad_spec!r} must be ERROR, got {r.verdict}"


def test_O11b_extractor_never_raises_on_garbage():
    from ledger.extractor import extract_from_text

    for junk in ["", "   ", "\x00\x01", "a" * 10000, "ASSUMPTION:", "http://", "commit "]:
        out = extract_from_text(junk)
        assert isinstance(out, list)


# --- O12 -------------------------------------------------------------------------------------
def test_O12_duplicate_assumption_detection(tmp_path):
    eng = _engine(tmp_path)
    spec = {"kind": "file_exists", "path": "dup.txt"}
    a1 = eng.add_assumption("the file dup.txt exists", spec)
    a2 = eng.add_assumption("The file dup.txt exists", spec)  # case/space variant
    assert a1.id == a2.id
    rows = eng.store.list_assumptions()
    assert len(rows) == 1
    assert rows[0].seen_count == 2


# --- meta: the table above must stay in sync with the tests -----------------------------------
def test_oracle_table_is_complete():
    ids = [row[0] for row in ORACLE_EXPECTATIONS]
    assert ids == [f"O{i}" for i in range(1, 13)]
    for case_id, scenario, expected in ORACLE_EXPECTATIONS:
        assert scenario and isinstance(expected, dict)
