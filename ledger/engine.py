"""The Assumption Ledger engine: capture, verify, propagate.

This is the single place that decides an assumption's lifecycle status. Everything else
(extractor, verifiers, store) is a pure helper; the engine owns the rules.

Lifecycle rules
---------------
* A check ``pass``  -> ``VALID``.
* A check ``fail``  -> ``INVALIDATED`` (definitive contradiction), with a reason recorded.
* A check ``error`` -> ``UNVERIFIED`` when we hold no prior evidence, ``STALE`` when we do.
  Provider errors, timeouts and rate limits therefore never manufacture an invalidation.
* A previously ``VALID`` assumption whose evidence is older than ``freshness_seconds``
  degrades to ``STALE``. A pass is never assumed to hold forever.

Invalidation propagation
------------------------
When an assumption becomes ``INVALIDATED``, every decision that depends on it is marked
``blocked`` with a reason naming the assumption. Decisions with no broken dependency are
untouched, so unrelated work is never silently blocked.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from . import model as M
from .extractor import Extracted, extract_from_text, extract_from_tool_result
from .model import (
    DROPPED,
    DROPPED_EVIDENCE,
    INVALIDATED,
    LIVE_STATUSES,
    STALE,
    UNVERIFIED,
    VALID,
    Assumption,
    CheckResult,
    Decision,
    decision_id,
)
from .store import LedgerStore
from .verifiers import CheckContext, SUPPORTED_KINDS, run_check

DEFAULT_FRESHNESS_SECONDS = 24 * 60 * 60  # one day


class LedgerEngine:
    def __init__(
        self,
        store: LedgerStore,
        *,
        freshness_seconds: float = DEFAULT_FRESHNESS_SECONDS,
        ctx: Optional[CheckContext] = None,
        now: Optional[callable] = None,
        reinject_limit: int = 25,
        auto_check_on_capture: bool = False,
    ):
        self.store = store
        self.freshness_seconds = float(freshness_seconds)
        self.ctx = ctx or CheckContext()
        self._now = now or time.time
        # Both read from plugin config by the integration; defaults match the documented values.
        self.reinject_limit = max(1, int(reinject_limit))
        self.auto_check_on_capture = bool(auto_check_on_capture)

    # --- capture ----------------------------------------------------------------------
    def observe_message(self, text: str, *, origin: str = "assistant", meta: Optional[Dict] = None) -> List[Assumption]:
        """Capture assumptions from the assistant's own message.

        The assistant's reply is the model's own statement, so a marker here is the agent
        declaring an assumption about its plan. This is the only text source that persists.
        """
        captured = self._ingest(extract_from_text(text or "", source=origin), origin, meta)
        self._auto_check(captured)
        return captured

    def observe_tool_result(self, tool_name: str, result: str, meta: Optional[Dict] = None) -> List[Assumption]:
        """Tool output is never persisted as an assumption.

        Tool output is untrusted: a web page, a file or a terminal listing can contain the literal
        text ``Assumption: ...`` and would otherwise write a durable record that is re-injected
        into every later turn. That is a prompt-injection channel into persistent context, so it is
        refused outright rather than filtered.

        The method is kept (and returns ``[]``) so a caller written against the previous API keeps
        working. Assumptions about a tool result belong in the assistant's own message, or in an
        explicit ``assumption_record`` call.
        """
        return []

    def add_assumption(
        self,
        statement: str,
        check: Dict[str, Any],
        *,
        origin: str = "manual",
        meta: Optional[Dict] = None,
        auto_check: Optional[bool] = None,
    ) -> Assumption:
        """Explicitly record one assumption (used by the tool and tests)."""
        asm = self._store_one(
            Extracted(statement=statement, check=check, origin=origin), origin, meta
        )
        should_check = self.auto_check_on_capture if auto_check is None else bool(auto_check)
        if should_check:
            self.verify(asm.id)
            refreshed = self.store.get_assumption(asm.id)
            if refreshed is not None:
                return refreshed
        return asm

    def _auto_check(self, captured: List[Assumption]) -> None:
        if not self.auto_check_on_capture:
            return
        for asm in captured:
            try:
                self.verify(asm.id)
            except Exception:  # a failed auto-check must never break capture
                continue

    def _ingest(
        self, items: List[Extracted], origin: str, meta: Optional[Dict]
    ) -> List[Assumption]:
        return [self._store_one(item, origin, meta) for item in items]

    def _store_one(self, item: Extracted, origin: str, meta: Optional[Dict]) -> Assumption:
        statement = item.statement
        check = item.check or {"kind": "unsupported"}
        asm_id = M.assumption_id(statement, check)
        provenance = {
            "origin": origin,
            "extractor": item.origin,
            "source_text": (item.source_text or "")[:500],
            "recorded_at": self._now(),
        }
        if meta:
            provenance.update(meta)
        asm = Assumption(
            id=asm_id,
            key=M.slug(statement),
            statement=statement,
            check=check,
            status=UNVERIFIED,
            provenance=provenance,
            created_at=self._now(),
            updated_at=self._now(),
        )
        return self.store.upsert_assumption(asm)

    # --- decisions --------------------------------------------------------------------
    def record_decision(
        self,
        statement: str,
        depends_on: Optional[List[str]] = None,
        *,
        origin: str = "manual",
        session_id: str = "",
    ) -> Decision:
        """Record a plan step / action and the assumptions it relies on.

        ``session_id`` scopes the decision. A blocker is only ever reported to the session that
        recorded the decision, so one session's contradiction cannot stop another's work.
        """
        dec = Decision(
            id=decision_id(statement),
            key=M.slug(statement),
            statement=statement,
            depends_on=list(depends_on or []),
            state="open",
            created_at=self._now(),
            session_id=str(session_id or ""),
        )
        stored = self.store.upsert_decision(dec)
        self._recompute_decision_block(stored)
        return self.store.get_decision(stored.id) or stored

    def link_decision(self, decision_id_value: str, assumption_ids: List[str]) -> Optional[Decision]:
        dec = self.store.get_decision(decision_id_value)
        if dec is None:
            return None
        dec.depends_on = sorted(set(dec.depends_on) | set(assumption_ids))
        self.store.save_decision(dec)
        self._recompute_decision_block(dec)
        return self.store.get_decision(dec.id)

    def _recompute_decision_block(self, dec: Decision) -> None:
        """A decision is blocked iff at least one dependency is INVALIDATED."""
        broken: List[Assumption] = []
        for asm_id in dec.depends_on:
            asm = self.store.get_assumption(asm_id)
            if asm is not None and asm.status == INVALIDATED:
                broken.append(asm)
        if broken:
            dec.state = "blocked"
            names = "; ".join(f"{a.id} ({a.statement[:60]})" for a in broken)
            dec.blocked_reason = f"depends on invalidated assumption(s): {names}"
        else:
            dec.state = "open"
            dec.blocked_reason = None
        self.store.save_decision(dec)

    # --- user recovery -----------------------------------------------------------------
    def resolve(self, asm_id: str, *, note: str = "") -> Dict[str, Any]:
        """Re-check an assumption and report the outcome. Never forces a status.

        This is the evidence path out of a block: the user asks for a re-check, the verifier runs,
        and the lifecycle rules decide. A pass clears the contradiction; a fail keeps it; an error
        leaves it exactly as it was. The user cannot assert a verdict here, which is what stops
        "resolve" from becoming a way to launder a contradiction into VALID.
        """
        asm = self.store.get_assumption(asm_id)
        if asm is None:
            return {"error": f"unknown assumption {asm_id}"}
        before = asm.status
        result = self.verify(asm_id)
        after = result.get("status", before)
        outcome = "reverified"
        if after == before == INVALIDATED:
            outcome = "still_invalidated"
        elif before == INVALIDATED and after != INVALIDATED:
            outcome = "cleared"
        return {
            "assumption": asm_id,
            "outcome": outcome,
            "previous_status": before,
            "status": after,
            "verdict": result.get("verdict"),
            "detail": result.get("detail"),
            "note": note or None,
        }

    def drop(self, asm_id: str, *, note: str = "") -> Dict[str, Any]:
        """Retire an assumption from the live set without claiming it was proven.

        DROPPED is a distinct status, not VALID. The assumption stops being re-injected and stops
        blocking dependent decisions, and the evidence trail records that a *user* retired it. No
        pass verdict is written, so the ledger never reports verification that did not happen.
        """
        asm = self.store.get_assumption(asm_id)
        if asm is None:
            return {"error": f"unknown assumption {asm_id}"}
        before = asm.status
        asm.status = DROPPED
        asm.updated_at = self._now()
        self.store.save_assumption(asm)
        self.store.add_evidence(
            asm.id,
            DROPPED_EVIDENCE,
            f"retired by user without verification{(': ' + note) if note else ''}",
            source="engine:drop",
        )
        unblocked = []
        for dec in self.store.decisions_depending_on(asm.id):
            self._recompute_decision_block(dec)
            unblocked.append(dec.id)
        return {
            "assumption": asm_id,
            "outcome": "dropped",
            "previous_status": before,
            "status": DROPPED,
            "unblocked_decisions": unblocked,
            "note": note or None,
        }

    # --- verification -----------------------------------------------------------------
    def verify(self, asm_id: str) -> Dict[str, Any]:
        """Run one assumption's check and apply the lifecycle rules."""
        asm = self.store.get_assumption(asm_id)
        if asm is None:
            return {"error": f"unknown assumption {asm_id}"}
        if asm.check.get("kind") == "unsupported":
            # No deterministic check exists; stay honestly UNVERIFIED.
            asm.updated_at = self._now()
            asm.last_checked_at = self._now()
            asm.check_count += 1
            self.store.save_assumption(asm)
            self.store.add_evidence(
                asm.id, "error", "no deterministic check available for this assumption",
                source="engine",
            )
            return {"assumption": asm.id, "status": asm.status, "verdict": "error",
                    "detail": "unsupported check"}
        result: CheckResult = run_check(asm.check, self.ctx)
        return self._apply_result(asm, result)

    def verify_all(self, statuses: Optional[tuple] = None) -> List[Dict[str, Any]]:
        """Verify every assumption (optionally filtered by current status)."""
        out = []
        for asm in self.store.list_assumptions():
            if statuses and asm.status not in statuses:
                continue
            out.append(self.verify(asm.id))
        return out

    def _apply_result(self, asm: Assumption, result: CheckResult) -> Dict[str, Any]:
        had_fresh = asm.is_fresh(self.freshness_seconds, self._now())
        new_status = M.status_after_check(asm.status, result.verdict, had_fresh_evidence=had_fresh)
        asm.status = new_status
        asm.updated_at = self._now()
        asm.last_checked_at = asm.updated_at
        asm.check_count += 1

        if result.verdict == M.PASS:
            asm.invalidated_reason = None
            asm.invalidated_at = None
        elif result.verdict == M.FAIL:
            asm.fail_count += 1
            asm.invalidated_reason = result.detail or "check failed"
            asm.invalidated_at = asm.updated_at
        # ERROR leaves any prior invalidation untouched: uncertainty does not overwrite history.

        self.store.save_assumption(asm)
        self.store.add_evidence(
            asm.id, result.verdict, result.detail, result.observed, result.source
        )

        # Keep dependent decisions in step on *every* outcome, so a repaired assumption
        # unblocks the work it was holding back, not just a contradiction blocking it.
        for dec in self.store.decisions_depending_on(asm.id):
            self._recompute_decision_block(dec)

        return {
            "assumption": asm.id,
            "status": asm.status,
            "verdict": result.verdict,
            "detail": result.detail,
            "observed": result.observed,
        }

    def _propagate_invalidation(self, asm: Assumption) -> List[str]:
        blocked = []
        for dec in self.store.decisions_depending_on(asm.id):
            self._recompute_decision_block(dec)
            blocked.append(dec.id)
        return blocked

    # --- freshness --------------------------------------------------------------------
    def sweep_stale(self) -> List[str]:
        """Degrade VALID assumptions whose evidence has aged out to STALE."""
        degraded = []
        for asm in self.store.list_assumptions(status=VALID):
            if not asm.is_fresh(self.freshness_seconds, self._now()):
                asm.status = STALE
                asm.updated_at = self._now()
                self.store.save_assumption(asm)
                self.store.add_evidence(
                    asm.id, "error",
                    f"evidence older than freshness window ({self.freshness_seconds:.0f}s)",
                    source="engine:freshness",
                )
                degraded.append(asm.id)
        return degraded

    # --- reporting --------------------------------------------------------------------
    def summary(self) -> Dict[str, Any]:
        """Counts and the records a caller needs to render a status view."""
        self.sweep_stale()
        assumptions = self.store.list_assumptions()
        by_status: Dict[str, int] = {s: 0 for s in M.STATUSES}
        for a in assumptions:
            by_status[a.status] = by_status.get(a.status, 0) + 1
        blocked = self.blocked_decisions()
        return {
            "total": len(assumptions),
            "by_status": by_status,
            "invalidated": [a.to_dict() for a in assumptions if a.status == INVALIDATED],
            "unverified": [a.to_dict() for a in assumptions if a.status == UNVERIFIED],
            "stale": [a.to_dict() for a in assumptions if a.status == STALE],
            "dropped": [a.to_dict() for a in assumptions if a.status == DROPPED],
            "blocked_decisions": [d.to_dict() for d in blocked],
            "supported_check_kinds": list(SUPPORTED_KINDS),
        }

    def critical_blockers(self, session_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Invalidated assumptions that block at least one decision, for the pre-proceed gate.

        ``session_id`` scopes the answer. When given, only decisions recorded in that session are
        considered, so a contradiction raised in one session never stops another. ``None`` (or an
        empty string, meaning "no session context") reports every blocked decision, which is what
        a status view wants.
        """
        out = []
        for asm in self.store.list_assumptions(status=INVALIDATED):
            decs = self.store.decisions_depending_on(asm.id)
            if session_id:
                decs = [d for d in decs if d.session_id == session_id]
            if decs:
                out.append({
                    "assumption": asm.to_dict(),
                    "decisions": [d.to_dict() for d in decs],
                })
        return out

    def blocked_decisions(self, session_id: Optional[str] = None) -> List[Decision]:
        blocked = [d for d in self.store.list_decisions() if d.state == "blocked"]
        if session_id:
            blocked = [d for d in blocked if d.session_id == session_id]
        return blocked

    def live_assumptions(self) -> List[Assumption]:
        """Assumptions the agent should still honour: VALID, STALE or UNVERIFIED.

        This is the re-injection payload. It is read entirely from durable storage, so it is
        available after a compaction, a restart, or a new session with no in-memory state. A
        DROPPED assumption is excluded: the user retired it, so it is no longer in effect.
        """
        return [a for a in self.store.list_assumptions() if a.status in LIVE_STATUSES]

    def render_reinjection(self, limit: Optional[int] = None) -> str:
        """Compact block re-injected into the model context to keep assumptions in view."""
        limit = self.reinject_limit if limit is None else limit
        live = self.live_assumptions()
        if not live:
            return ""
        lines = ["Assumption Ledger — operational assumptions currently in effect:"]
        for a in live[:limit]:
            lines.append(f"  [{a.status}] {a.statement}  (id {a.id})")
        broken = self.critical_blockers()
        if broken:
            lines.append("Invalidated assumptions blocking planned work:")
            for b in broken[:limit]:
                lines.append(
                    f"  [INVALIDATED] {b['assumption']['statement']} (id {b['assumption']['id']})"
                )
            lines.append("  Use /assumptions show <id>, /assumptions resolve <id> or "
                         "/assumptions drop <id> to inspect or clear a block.")
        return "\n".join(lines)

    def render_status(self, limit: int = 20) -> str:
        s = self.summary()
        lines = [
            "Assumption Ledger",
            f"  tracked: {s['total']}  "
            + "  ".join(f"{k.lower()}={v}" for k, v in s["by_status"].items()),
        ]
        live = self.live_assumptions()
        if live:
            lines.append("  live assumptions (in effect):")
            for a in live[:limit]:
                lines.append(f"    - [{a.status}] {a.id} {a.statement[:70]}")
        if s["invalidated"]:
            lines.append("  INVALIDATED (contradicted by evidence):")
            for a in s["invalidated"][:limit]:
                lines.append(f"    - {a['id']} {a['statement'][:70]} — {a['invalidated_reason']}")
        if s["blocked_decisions"]:
            lines.append("  BLOCKED decisions:")
            for d in s["blocked_decisions"][:limit]:
                scope = f" [session {d['session_id'][:8]}]" if d.get("session_id") else ""
                lines.append(f"    - {d['id']} {d['statement'][:70]}{scope}")
            lines.append("  To clear a block: /assumptions resolve <id> (re-check) or "
                         "/assumptions drop <id> (retire without verification).")
        if s["stale"]:
            lines.append(f"  STALE: {len(s['stale'])} assumption(s) need re-checking")
        if s["unverified"]:
            lines.append(f"  UNVERIFIED: {len(s['unverified'])} assumption(s) have no evidence yet")
        if s.get("dropped"):
            lines.append(f"  DROPPED: {len(s['dropped'])} assumption(s) retired by the user")
        if not s["total"]:
            lines.append("  (nothing captured yet)")
        return "\n".join(lines)

    def render_assumption(self, asm_id: str) -> str:
        asm = self.store.get_assumption(asm_id)
        if asm is None:
            return f"No assumption {asm_id}"
        ev = self.store.evidence_for(asm.id, limit=5)
        decs = self.store.decisions_depending_on(asm.id)
        lines = [
            f"{asm.id}  [{asm.status}]",
            f"  statement: {asm.statement}",
            f"  check: {asm.check}",
            f"  provenance: {asm.provenance.get('origin', '?')} "
            f"(extractor={asm.provenance.get('extractor', '?')})",
            f"  created: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(asm.created_at))}",
            f"  checks: {asm.check_count}  fails: {asm.fail_count}  seen: {asm.seen_count}",
        ]
        if asm.invalidated_reason:
            lines.append(f"  invalidated: {asm.invalidated_reason}")
        if decs:
            lines.append("  dependent decisions:")
            for d in decs:
                lines.append(f"    - {d.id} [{d.state}] {d.statement[:60]}")
        if ev:
            lines.append("  evidence (newest first):")
            for e in ev:
                lines.append(f"    - {e.verdict}: {e.detail[:80]}")
        return "\n".join(lines)
