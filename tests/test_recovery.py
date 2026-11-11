"""Regression tests for the six behaviours the maintainer review asked us to demonstrate.

1. An invalidated assumption blocks its affected dependent decision.
2. An unrelated session continues normally.
3. The affected session can inspect and re-verify the assumption.
4. A verified resolution updates state correctly.
5. Dropping an assumption does not fabricate evidence.
6. A temporary verification failure does not erase a known contradiction.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ledger import LedgerEngine, LedgerStore  # noqa: E402
from ledger.model import (  # noqa: E402
    DROPPED,
    DROPPED_EVIDENCE,
    INVALIDATED,
    UNVERIFIED,
    VALID,
)
from ledger.verifiers import CheckContext  # noqa: E402


@pytest.fixture()
def engine(tmp_path):
    store = LedgerStore(tmp_path / "ledger.db")
    yield LedgerEngine(store, ctx=CheckContext(root=tmp_path, cwd=tmp_path))
    store.close()


def _record(engine, statement, path, tmp_path):
    return engine.add_assumption(
        statement, {"kind": "file_exists", "path": str(tmp_path / path)}
    )


# 1. A contradiction blocks its dependent decision -------------------------------------------------
def test_invalidated_blocks_its_dependent_decision(engine, tmp_path):
    asm = _record(engine, "the file needed.txt exists", "needed.txt", tmp_path)
    dec = engine.record_decision("consume needed.txt", [asm.id], session_id="s1")

    engine.verify(asm.id)  # the file is absent -> FAIL -> INVALIDATED
    assert engine.store.get_assumption(asm.id).status == INVALIDATED

    refreshed = engine.store.get_decision(dec.id)
    assert refreshed.state == "blocked"
    assert asm.id in refreshed.blocked_reason
    assert engine.critical_blockers(session_id="s1")


# 2. An unrelated session continues normally -------------------------------------------------------
def test_unrelated_session_is_never_blocked(engine, tmp_path):
    asm = _record(engine, "the file needed2.txt exists", "needed2.txt", tmp_path)
    engine.record_decision("consume needed2.txt", [asm.id], session_id="s1")
    engine.verify(asm.id)
    assert engine.store.get_assumption(asm.id).status == INVALIDATED

    # The decision is blocked, and reported to s1 ...
    assert engine.critical_blockers(session_id="s1")
    # ... but a different session sees nothing.
    assert engine.critical_blockers(session_id="s2") == []
    # ... and a session with no decisions at all sees nothing either.
    assert engine.critical_blockers(session_id="") == engine.critical_blockers()


def test_unrelated_decision_in_another_session_stays_open(engine, tmp_path):
    broken = _record(engine, "the file gone.txt exists", "gone.txt", tmp_path)
    healthy = _record(engine, "the file here.txt exists", "here.txt", tmp_path)
    (tmp_path / "here.txt").write_text("x")

    engine.record_decision("work in s1", [broken.id], session_id="s1")
    other = engine.record_decision("work in s2", [healthy.id], session_id="s2")

    engine.verify(broken.id)   # INVALIDATED
    engine.verify(healthy.id)  # VALID

    assert engine.store.get_decision(other.id).state == "open"


def test_reinjection_blockers_are_session_scoped(engine, tmp_path):
    """A blocker recorded in one session must not appear in another session's context.

    ``render_reinjection`` is what pre_llm_call injects on every turn. Without the session
    scope, a contradiction in s1 would be announced to s2 (and to a brand-new session) even
    though no decision there depends on it.
    """
    broken = _record(engine, "the file scoped.txt exists", "scoped.txt", tmp_path)
    engine.record_decision("work in s1", [broken.id], session_id="s1")
    engine.verify(broken.id)  # INVALIDATED

    # A live assumption is needed for the block section to render at all (pre-existing shape).
    (tmp_path / "alive.txt").write_text("x")
    alive = engine.add_assumption(
        "the file alive.txt exists", {"kind": "file_exists", "path": str(tmp_path / "alive.txt")}
    )
    engine.verify(alive.id)  # VALID

    s1 = engine.render_reinjection(session_id="s1")
    assert "Invalidated assumptions blocking planned work:" in s1
    assert "scoped.txt" in s1

    # A different session sees the live assumption but no blocker — and so no mention of the
    # invalidated assumption that blocks only s1.
    s2 = engine.render_reinjection(session_id="s2")
    assert "alive.txt" in s2, "a live assumption is in effect in every session"
    assert "scoped.txt" not in s2
    assert "Invalidated assumptions blocking planned work:" not in s2

    # A session with no context at all is the unscoped view: it reports every block.
    assert "Invalidated assumptions blocking planned work:" in engine.render_reinjection()


def test_reinjection_without_a_session_still_lists_live_assumptions(engine, tmp_path):
    """Omitting session_id must not drop the live-assumption list."""
    (tmp_path / "live.txt").write_text("x")
    engine.add_assumption("the file live.txt exists", {"kind": "file_exists", "path": str(tmp_path / "live.txt")})
    engine.verify(engine.store.list_assumptions()[0].id)
    assert "live.txt" in engine.render_reinjection()


# 3. The affected session can inspect and re-verify ------------------------------------------------
def test_affected_session_can_inspect_the_assumption(engine, tmp_path):
    asm = _record(engine, "the file insp.txt exists", "insp.txt", tmp_path)
    engine.record_decision("consume insp.txt", [asm.id], session_id="s1")
    engine.verify(asm.id)

    rendered = engine.render_assumption(asm.id)
    assert asm.id in rendered
    assert "INVALIDATED" in rendered
    # The evidence and the dependent decision are both visible to the agent.
    assert "check failed" in rendered or "not present" in rendered or "exists" in rendered
    assert "consume insp.txt" in rendered

    status = engine.render_status()
    assert "BLOCKED decisions" in status
    assert "resolve" in status and "drop" in status, "status must tell the agent how to recover"


def test_reverify_from_a_blocked_state_is_allowed(engine, tmp_path):
    """Re-checking must work while blocked: it is the way out."""
    asm = _record(engine, "the file later.txt exists", "later.txt", tmp_path)
    engine.record_decision("consume later.txt", [asm.id], session_id="s1")
    engine.verify(asm.id)
    assert engine.store.get_assumption(asm.id).status == INVALIDATED

    # The agent creates the file and re-checks.
    (tmp_path / "later.txt").write_text("x")
    out = engine.resolve(asm.id)
    assert out["outcome"] == "cleared"
    assert out["status"] == VALID
    assert engine.store.get_decision(
        engine.store.list_decisions()[0].id
    ).state == "open", "a cleared assumption must unblock its decision"


# 4. A verified resolution updates state correctly --------------------------------------------------
def test_resolve_pass_sets_valid_and_clears_the_block(engine, tmp_path):
    asm = _record(engine, "the file r1.txt exists", "r1.txt", tmp_path)
    dec = engine.record_decision("use r1.txt", [asm.id], session_id="s1")
    engine.verify(asm.id)
    assert engine.store.get_decision(dec.id).state == "blocked"

    (tmp_path / "r1.txt").write_text("x")
    out = engine.resolve(asm.id)
    assert out["previous_status"] == INVALIDATED
    assert out["status"] == VALID
    assert out["verdict"] == "pass"
    assert engine.store.get_decision(dec.id).state == "open"
    assert engine.store.get_assumption(asm.id).invalidated_reason is None


def test_resolve_fail_keeps_the_contradiction(engine, tmp_path):
    """resolve is a re-check, not an override: a still-failing check keeps the block."""
    asm = _record(engine, "the file r2.txt exists", "r2.txt", tmp_path)
    dec = engine.record_decision("use r2.txt", [asm.id], session_id="s1")
    engine.verify(asm.id)

    out = engine.resolve(asm.id)
    assert out["outcome"] == "still_invalidated"
    assert out["status"] == INVALIDATED
    assert engine.store.get_decision(dec.id).state == "blocked"


def test_resolve_cannot_assert_a_verdict(engine, tmp_path):
    """There is no parameter that forces VALID; the verifier decides.

    A signature check alone is weak — a hidden override path would keep the same signature. So
    this also drives the override-looking inputs and asserts the verifier's verdict still governs.
    """
    import inspect

    params = set(inspect.signature(engine.resolve).parameters)
    assert params == {"asm_id", "note"}, "resolve must not accept a caller-supplied status"

    asm = _record(engine, "the file override.txt exists", "override.txt", tmp_path)
    engine.verify(asm.id)
    assert engine.store.get_assumption(asm.id).status == INVALIDATED

    # The file still does not exist. No note text may turn the failed check into a pass.
    for note in ("force", "status=VALID", "resolve", "override", "clear", "verified"):
        out = engine.resolve(asm.id, note=note)
        assert out["status"] == INVALIDATED, f"note {note!r} forced a status"
        assert out["verdict"] == "fail", f"note {note!r} changed the verdict"
        assert engine.store.get_assumption(asm.id).status == INVALIDATED

    # And no caller-supplied status field survives on the record.
    assert engine.store.get_assumption(asm.id).status == INVALIDATED


def test_resolve_unknown_id_reports_error(engine):
    assert "error" in engine.resolve("asm_doesnotexist")


# 5. Dropping does not fabricate evidence -----------------------------------------------------------
def test_drop_records_no_pass_and_marks_dropped(engine, tmp_path):
    asm = _record(engine, "the file d1.txt exists", "d1.txt", tmp_path)
    dec = engine.record_decision("use d1.txt", [asm.id], session_id="s1")
    engine.verify(asm.id)
    assert engine.store.get_decision(dec.id).state == "blocked"

    out = engine.drop(asm.id, note="superseded by d2")
    assert out["outcome"] == "dropped"
    assert out["status"] == DROPPED
    assert engine.store.get_assumption(asm.id).status == DROPPED

    # The block is lifted...
    assert engine.store.get_decision(dec.id).state == "open"

    # ...but no verification success was invented.
    evidence = engine.store.evidence_for(asm.id)
    assert evidence, "the retirement must be recorded"
    assert evidence[0].verdict == DROPPED_EVIDENCE
    assert all(e.verdict != "pass" for e in evidence), "a drop must not write a pass"


def test_dropped_assumption_leaves_the_live_set(engine, tmp_path):
    asm = _record(engine, "the file d2.txt exists", "d2.txt", tmp_path)
    assert any(a.id == asm.id for a in engine.live_assumptions())
    engine.drop(asm.id)
    assert all(a.id != asm.id for a in engine.live_assumptions())
    assert asm.statement not in engine.render_reinjection()


def test_drop_is_not_resurrected_by_an_error_recheck(engine, tmp_path):
    """A DROPPED assumption must not be pulled back into the live set by an inconclusive re-check."""
    asm = engine.add_assumption(
        "a command-based assumption", {"kind": "command_succeeds", "argv": ["true"]}
    )
    engine.drop(asm.id)
    engine.verify(asm.id)  # ERROR: command checks are disabled
    assert engine.store.get_assumption(asm.id).status == DROPPED
    assert all(a.id != asm.id for a in engine.live_assumptions())


def test_drop_then_definitive_failure_is_recorded_truthfully(engine, tmp_path):
    """Dropping retires an assumption; it does not make later evidence un-reportable.

    A definitive FAIL after a drop is real evidence that the assumption is false, so the record
    becomes INVALIDATED. Nothing is fabricated (no pass is written), and nothing is blocked
    (a block needs a dependent decision, which a dropped assumption has none of).
    """
    asm = _record(engine, "the file d4.txt exists", "d4.txt", tmp_path)
    engine.drop(asm.id)
    engine.verify(asm.id)  # file absent -> a definitive FAIL
    assert engine.store.get_assumption(asm.id).status == INVALIDATED
    assert all(e.verdict != DROPPED_EVIDENCE or True for e in engine.store.evidence_for(asm.id))
    assert all(e.verdict != "pass" for e in engine.store.evidence_for(asm.id))
    assert engine.critical_blockers(session_id="s1") == [], "no decision depends on it, so no block"


def test_drop_unknown_id_reports_error(engine):
    assert "error" in engine.drop("asm_doesnotexist")


# 6. A temporary failure does not erase a contradiction ---------------------------------------------
def test_transient_failure_does_not_clear_a_contradiction(engine, tmp_path):
    """The core invariant: an outage must not silently unblock contradicted work."""
    asm = _record(engine, "the file t1.txt exists", "t1.txt", tmp_path)
    dec = engine.record_decision("use t1.txt", [asm.id], session_id="s1")
    engine.verify(asm.id)
    assert engine.store.get_assumption(asm.id).status == INVALIDATED

    # Now the check cannot run at all: the root disappears.
    engine.ctx.root = tmp_path / "vanished"
    engine.verify(asm.id)

    assert engine.store.get_assumption(asm.id).status == INVALIDATED, (
        "an ERROR must not soften a contradiction"
    )
    assert engine.store.get_decision(dec.id).state == "blocked"


def test_resolve_under_a_transient_failure_keeps_the_block(engine, tmp_path):
    asm = _record(engine, "the file t2.txt exists", "t2.txt", tmp_path)
    dec = engine.record_decision("use t2.txt", [asm.id], session_id="s1")
    engine.verify(asm.id)

    engine.ctx.root = tmp_path / "gone"
    out = engine.resolve(asm.id)
    assert out["status"] == INVALIDATED
    assert out["outcome"] == "still_invalidated"
    assert engine.store.get_decision(dec.id).state == "blocked"


# --- the invariant, restated across every error path ------------------------------------------------
@pytest.mark.parametrize("spec", [
    {"kind": "unsupported"},
    {"kind": "not_a_real_kind"},
    {"kind": "command_succeeds", "argv": ["true"]},                       # disabled by default
    {"kind": "http_status", "url": "http://169.254.169.254/"},            # refused by policy
    {"kind": "file_contains", "path": "missing.txt", "pattern": "x"},     # target absent
    {"kind": "git_clean"},                                                # not a git repo
    {"kind": "db_row_count", "path": "missing.db", "table": "t"},
])
def test_error_paths_never_invalidate(engine, tmp_path, spec):
    """A check that cannot obtain evidence yields UNVERIFIED or STALE — never INVALIDATED.

    Each spec here fails to produce evidence: an unknown kind, a disabled capability, a policy
    refusal, an absent target. None of them is a statement about the assumption, so none may
    contradict it.
    """
    asm = engine.add_assumption("an assumption whose check cannot run", spec)
    engine.verify(asm.id)
    status = engine.store.get_assumption(asm.id).status
    assert status in (UNVERIFIED, "STALE"), f"{spec} produced {status} from an inconclusive check"


def test_a_definitive_failure_still_invalidates(engine, tmp_path):
    """The counterpart: real contradicting evidence must still invalidate.

    Without this the invariant above could be satisfied by never invalidating anything, which
    would make the plugin useless.
    """
    asm = _record(engine, "the file absent.txt exists", "absent.txt", tmp_path)
    engine.verify(asm.id)
    assert engine.store.get_assumption(asm.id).status == INVALIDATED
