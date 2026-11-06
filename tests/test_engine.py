"""Engine-level tests: lifecycle transitions, provenance, invalidation propagation, persistence."""
from __future__ import annotations

import sys
import time
from pathlib import Path

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
)
from ledger.verifiers import CheckContext  # noqa: E402


def _engine(tmp_path, freshness=3600.0, http_get=None):
    store = LedgerStore(tmp_path / "l.db")
    return LedgerEngine(
        store,
        freshness_seconds=freshness,
        ctx=CheckContext(root=tmp_path, cwd=tmp_path, http_get=http_get),
    )


def test_assumption_preserves_full_record(tmp_path):
    (tmp_path / "r.txt").write_text("x")
    eng = _engine(tmp_path)
    a = eng.add_assumption(
        "the file r.txt exists", {"kind": "file_exists", "path": "r.txt"},
        origin="tool", meta={"session_id": "s1"},
    )
    eng.verify(a.id)
    rec = eng.store.get_assumption(a.id)
    d = rec.to_dict()
    # Every field the mission requires must be present and populated.
    assert d["statement"] and d["check"] and d["status"] == VALID
    assert d["provenance"]["origin"] == "tool"
    assert d["provenance"]["session_id"] == "s1"
    assert d["created_at"] > 0
    assert d["last_checked_at"] is not None
    assert d["check_count"] == 1
    assert d["depends_on"] == []
    assert d["invalidated_reason"] is None
    assert len(eng.store.evidence_for(a.id)) == 1


def test_evidence_links_to_assumption(tmp_path):
    eng = _engine(tmp_path)
    a = eng.add_assumption("the file z.txt exists", {"kind": "file_exists", "path": "z.txt"})
    eng.verify(a.id)
    ev = eng.store.evidence_for(a.id)
    assert ev and ev[0].assumption_id == a.id
    assert ev[0].verdict == FAIL
    assert ev[0].observed.get("path")


def test_invalidation_reason_recorded(tmp_path):
    eng = _engine(tmp_path)
    a = eng.add_assumption("the file no.txt exists", {"kind": "file_exists", "path": "no.txt"})
    eng.verify(a.id)
    rec = eng.store.get_assumption(a.id)
    assert rec.invalidated_reason
    assert rec.invalidated_at is not None
    assert rec.fail_count == 1


def test_uncertainty_never_invalidates(tmp_path):
    def boom(u, t):
        raise TimeoutError("nope")

    eng = _engine(tmp_path, http_get=boom)
    a = eng.add_assumption(
        "the endpoint http://x is reachable",
        {"kind": "http_status", "url": "http://x", "expected_status": 200},
    )
    r = eng.verify(a.id)
    assert r["verdict"] == ERROR
    assert r["status"] == UNVERIFIED
    assert eng.store.get_assumption(a.id).invalidated_reason is None


def test_stale_then_revalidated(tmp_path):
    (tmp_path / "s.txt").write_text("x")
    eng = _engine(tmp_path, freshness=5.0)
    a = eng.add_assumption("the file s.txt exists", {"kind": "file_exists", "path": "s.txt"})
    assert eng.verify(a.id)["status"] == VALID
    rec = eng.store.get_assumption(a.id)
    rec.last_checked_at = time.time() - 50
    eng.store.save_assumption(rec)
    assert a.id in eng.sweep_stale()
    assert eng.store.get_assumption(a.id).status == STALE
    # Re-checking restores VALID.
    assert eng.verify(a.id)["status"] == VALID


def test_invalidation_propagates_only_to_dependents(tmp_path):
    (tmp_path / "ok.txt").write_text("x")
    eng = _engine(tmp_path)
    good = eng.add_assumption("the file ok.txt exists", {"kind": "file_exists", "path": "ok.txt"})
    bad = eng.add_assumption("the file bad.txt exists", {"kind": "file_exists", "path": "bad.txt"})
    eng.verify(good.id)
    dependent = eng.record_decision("use bad.txt", depends_on=[bad.id])
    independent = eng.record_decision("unrelated step", depends_on=[good.id])
    eng.verify(bad.id)
    assert eng.store.get_decision(dependent.id).state == "blocked"
    assert eng.store.get_decision(independent.id).state == "open"


def test_decision_recovers_when_assumption_repaired(tmp_path):
    eng = _engine(tmp_path)
    target = tmp_path / "fix.txt"
    a = eng.add_assumption("the file fix.txt exists", {"kind": "file_exists", "path": "fix.txt"})
    eng.verify(a.id)  # FAIL
    dec = eng.record_decision("consume fix.txt", depends_on=[a.id])
    assert eng.store.get_decision(dec.id).state == "blocked"
    target.write_text("now present")
    eng.verify(a.id)  # PASS
    assert eng.store.get_decision(dec.id).state == "open"
    assert eng.store.get_decision(dec.id).blocked_reason is None


def test_critical_blockers_lists_only_actionable(tmp_path):
    eng = _engine(tmp_path)
    a = eng.add_assumption("the file gone.txt exists", {"kind": "file_exists", "path": "gone.txt"})
    eng.verify(a.id)
    # No decision depends on it yet -> not a blocker.
    assert eng.critical_blockers() == []
    eng.record_decision("use gone.txt", depends_on=[a.id])
    blockers = eng.critical_blockers()
    assert len(blockers) == 1
    assert blockers[0]["assumption"]["id"] == a.id


def test_duplicate_detection_across_capture_paths(tmp_path):
    eng = _engine(tmp_path)
    a = eng.add_assumption("the file d.txt exists", {"kind": "file_exists", "path": "d.txt"})
    eng.observe_message("ASSUMPTION: the file d.txt exists")
    rows = eng.store.list_assumptions()
    assert len(rows) == 1
    assert rows[0].seen_count == 2


def test_unsupported_check_stays_unverified(tmp_path):
    eng = _engine(tmp_path)
    a = eng.add_assumption("the system is healthy", {"kind": "unsupported"})
    r = eng.verify(a.id)
    assert r["status"] == UNVERIFIED
    assert r["verdict"] == "error"


def test_summary_shape(tmp_path):
    (tmp_path / "ok.txt").write_text("x")
    eng = _engine(tmp_path)
    eng.add_assumption("the file ok.txt exists", {"kind": "file_exists", "path": "ok.txt"})
    eng.add_assumption("the file no.txt exists", {"kind": "file_exists", "path": "no.txt"})
    eng.verify_all()
    s = eng.summary()
    assert s["total"] == 2
    assert s["by_status"][VALID] == 1
    assert s["by_status"][INVALIDATED] == 1
    assert len(s["invalidated"]) == 1
    assert s["supported_check_kinds"]


def test_provenance_accumulates_on_reobservation(tmp_path):
    eng = _engine(tmp_path)
    eng.add_assumption("the file p.txt exists", {"kind": "file_exists", "path": "p.txt"}, origin="a")
    eng.add_assumption("the file p.txt exists", {"kind": "file_exists", "path": "p.txt"}, origin="b")
    rec = eng.store.get_assumption(eng.store.list_assumptions()[0].id)
    assert rec.provenance["first_origin"]["origin"] == "a", "first sighting is preserved"
    origins = [o.get("origin") for o in rec.provenance["origins"]]
    assert origins == ["a", "b"], "each origin is recorded once, in order"


def test_render_helpers_do_not_raise(tmp_path):
    eng = _engine(tmp_path)
    assert eng.render_reinjection() == ""  # nothing to inject
    eng.add_assumption("the file x.txt exists", {"kind": "file_exists", "path": "x.txt"})
    assert "x.txt" in eng.render_reinjection()
    assert "Assumption Ledger" in eng.render_status()
    assert "x.txt" in eng.render_assumption(eng.store.list_assumptions()[0].id)
    assert "No assumption" in eng.render_assumption("asm_missing")


def test_verify_unknown_id_is_safe(tmp_path):
    eng = _engine(tmp_path)
    assert "error" in eng.verify("asm_nope")
