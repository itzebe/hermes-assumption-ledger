"""Core records for the Assumption Ledger: lifecycle states, assumptions, evidence, decisions.

Everything here works on *observable* agent information only (messages, tool results,
filesystem, git, HTTP, local databases). Nothing in this module reads or claims to read
private model reasoning.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# --- Lifecycle states (Phase 2) -------------------------------------------------------------
# UNVERIFIED is the safe default. A provider error, timeout, rate limit or missing evidence
# must NEVER produce INVALIDATED.
VALID = "VALID"
INVALIDATED = "INVALIDATED"
UNVERIFIED = "UNVERIFIED"
STALE = "STALE"
STATUSES = (VALID, INVALIDATED, UNVERIFIED, STALE)

# --- Verifier verdicts (internal to one check attempt) --------------------------------------
PASS = "pass"
FAIL = "fail"
ERROR = "error"  # timeout / rate limit / provider unavailable / missing evidence

VERDICTS = (PASS, FAIL, ERROR)

_ID_SAFE = re.compile(r"[^a-z0-9]+")


def _now() -> float:
    return time.time()


def normalize_statement(text: str) -> str:
    """Whitespace/case-insensitive form used for duplicate detection."""
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def slug(text: str, limit: int = 32) -> str:
    s = _ID_SAFE.sub("-", (text or "").strip().lower()).strip("-")
    return s[:limit] or "item"


def canonical_check_key(check: Optional[Dict[str, Any]]) -> str:
    """Stable serialization of a check spec, used for the assumption identity hash."""
    check = check or {}
    params = {k: v for k, v in check.items() if k != "kind"}
    return str(check.get("kind", "")) + ":" + json.dumps(params, sort_keys=True, default=str)


def assumption_id(statement: str, check: Optional[Dict[str, Any]]) -> str:
    """Deterministic id: the same statement + same check spec always maps to one record."""
    key = normalize_statement(statement) + "|" + canonical_check_key(check)
    return "asm_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def decision_id(statement: str) -> str:
    return "dec_" + hashlib.sha256(normalize_statement(statement).encode("utf-8")).hexdigest()[:12]


def evidence_id(assumption: str, verdict: str, created_at: float, source: str) -> str:
    raw = f"{assumption}|{verdict}|{created_at:.6f}|{source}"
    return "ev_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


@dataclass
class CheckResult:
    """Outcome of a single deterministic check attempt."""

    verdict: str
    detail: str = ""
    observed: Dict[str, Any] = field(default_factory=dict)
    source: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict,
            "detail": self.detail,
            "observed": self.observed,
            "source": self.source,
        }


@dataclass
class Assumption:
    id: str
    statement: str
    check: Dict[str, Any]
    status: str = UNVERIFIED
    key: str = ""
    provenance: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=_now)
    updated_at: float = field(default_factory=_now)
    last_checked_at: Optional[float] = None
    invalidated_reason: Optional[str] = None
    invalidated_at: Optional[float] = None
    check_count: int = 0
    fail_count: int = 0
    seen_count: int = 1
    depends_on: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "key": self.key,
            "statement": self.statement,
            "check": self.check,
            "status": self.status,
            "provenance": self.provenance,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_checked_at": self.last_checked_at,
            "invalidated_reason": self.invalidated_reason,
            "invalidated_at": self.invalidated_at,
            "check_count": self.check_count,
            "fail_count": self.fail_count,
            "seen_count": self.seen_count,
            "depends_on": list(self.depends_on),
        }

    def is_fresh(self, freshness_seconds: float, now: Optional[float] = None) -> bool:
        """True when the last successful check is still inside the freshness window."""
        if self.last_checked_at is None:
            return False
        return ((now if now is not None else _now()) - self.last_checked_at) <= freshness_seconds


@dataclass
class Evidence:
    id: str
    assumption_id: str
    verdict: str
    detail: str
    observed: Dict[str, Any]
    source: str
    created_at: float = field(default_factory=_now)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "assumption_id": self.assumption_id,
            "verdict": self.verdict,
            "detail": self.detail,
            "observed": self.observed,
            "source": self.source,
            "created_at": self.created_at,
        }


@dataclass
class Decision:
    """A plan step or action that depends on one or more assumptions."""

    id: str
    statement: str
    depends_on: List[str] = field(default_factory=list)
    state: str = "open"  # open | blocked
    key: str = ""
    created_at: float = field(default_factory=_now)
    blocked_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "key": self.key,
            "statement": self.statement,
            "depends_on": list(self.depends_on),
            "state": self.state,
            "created_at": self.created_at,
            "blocked_reason": self.blocked_reason,
        }


def status_after_check(
    previous_status: str,
    verdict: str,
    *,
    had_fresh_evidence: bool,
) -> str:
    """Map a verifier verdict onto a lifecycle status.

    This is the single rule the whole engine obeys:

    * ``pass``  -> VALID
    * ``fail``  -> INVALIDATED (a definitive contradiction)
    * ``error`` -> UNVERIFIED, never INVALIDATED. If we previously held fresh evidence the
      record degrades to STALE instead: we still have the old evidence, we simply cannot
      refresh it right now.
    """
    if verdict == PASS:
        return VALID
    if verdict == FAIL:
        return INVALIDATED
    return STALE if had_fresh_evidence else UNVERIFIED
