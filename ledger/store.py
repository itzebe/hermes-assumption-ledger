"""Durable, process-safe persistence for ledger records.

Uses SQLite in WAL mode so records survive a restart and so a tool writer and a hook writer in
the same process can coexist. The store is storage-agnostic: it takes a path, so tests can point
it at a temp file and the Hermes integration can point it at the plugin data dir.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .model import Assumption, Decision, Evidence, evidence_id

_SCHEMA = """
CREATE TABLE IF NOT EXISTS assumptions (
    id TEXT PRIMARY KEY,
    key TEXT NOT NULL,
    statement TEXT NOT NULL,
    check_json TEXT NOT NULL,
    status TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    last_checked_at REAL,
    invalidated_reason TEXT,
    invalidated_at REAL,
    check_count INTEGER NOT NULL DEFAULT 0,
    fail_count INTEGER NOT NULL DEFAULT 0,
    seen_count INTEGER NOT NULL DEFAULT 1,
    depends_on_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_assumptions_key ON assumptions(key);

CREATE TABLE IF NOT EXISTS evidence (
    id TEXT PRIMARY KEY,
    assumption_id TEXT NOT NULL,
    verdict TEXT NOT NULL,
    detail TEXT NOT NULL,
    observed_json TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evidence_assumption ON evidence(assumption_id, created_at);

CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    key TEXT NOT NULL,
    statement TEXT NOT NULL,
    depends_on_json TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at REAL NOT NULL,
    blocked_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_decisions_key ON decisions(key);
"""


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _loads(text: Optional[str], fallback: Any) -> Any:
    if not text:
        return fallback
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return fallback


class LedgerStore:
    """Thread-safe SQLite-backed store for assumptions, evidence and decisions."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            # Network filesystems cannot do WAL; DELETE is a fine fallback.
            self._conn.execute("PRAGMA journal_mode=DELETE")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- assumptions ------------------------------------------------------------------
    def upsert_assumption(self, asm: Assumption) -> Assumption:
        """Insert a new assumption or merge into the existing record with the same id.

        A merge preserves created_at, provenance and any existing invalidation, and increments
        seen_count, so re-observing the same assumption is idempotent rather than duplicating.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM assumptions WHERE id = ?", (asm.id,)
            ).fetchone()
            if row is None:
                self._conn.execute(
                    """INSERT INTO assumptions (id, key, statement, check_json, status,
                       provenance_json, created_at, updated_at, last_checked_at,
                       invalidated_reason, invalidated_at, check_count, fail_count,
                       seen_count, depends_on_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        asm.id, asm.key, asm.statement, _dumps(asm.check), asm.status,
                        _dumps(asm.provenance), asm.created_at, asm.updated_at,
                        asm.last_checked_at, asm.invalidated_reason, asm.invalidated_at,
                        asm.check_count, asm.fail_count, asm.seen_count,
                        _dumps(asm.depends_on),
                    ),
                )
                self._conn.commit()
                return asm

            existing = self._row_to_assumption(row)
            existing.seen_count += 1
            existing.updated_at = asm.updated_at
            existing.statement = asm.statement or existing.statement
            # Provenance accumulates: keep the first sighting and append a new origin entry.
            existing.provenance = _merge_provenance(existing.provenance, asm.provenance)
            if asm.depends_on:
                existing.depends_on = sorted(set(existing.depends_on) | set(asm.depends_on))
            self._write_assumption(existing)
            return existing

    def _write_assumption(self, asm: Assumption) -> None:
        self._conn.execute(
            """UPDATE assumptions SET key=?, statement=?, check_json=?, status=?,
               provenance_json=?, updated_at=?, last_checked_at=?, invalidated_reason=?,
               invalidated_at=?, check_count=?, fail_count=?, seen_count=?, depends_on_json=?
               WHERE id=?""",
            (
                asm.key, asm.statement, _dumps(asm.check), asm.status,
                _dumps(asm.provenance), asm.updated_at, asm.last_checked_at,
                asm.invalidated_reason, asm.invalidated_at, asm.check_count,
                asm.fail_count, asm.seen_count, _dumps(asm.depends_on), asm.id,
            ),
        )
        self._conn.commit()

    def save_assumption(self, asm: Assumption) -> None:
        """Persist a mutated assumption record (status transitions, counters)."""
        with self._lock:
            self._write_assumption(asm)

    def get_assumption(self, asm_id: str) -> Optional[Assumption]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM assumptions WHERE id = ?", (asm_id,)
            ).fetchone()
        return self._row_to_assumption(row) if row else None

    def list_assumptions(self, status: Optional[str] = None) -> List[Assumption]:
        with self._lock:
            if status:
                rows = self._conn.execute(
                    "SELECT * FROM assumptions WHERE status = ? ORDER BY created_at",
                    (status,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM assumptions ORDER BY created_at"
                ).fetchall()
        return [self._row_to_assumption(r) for r in rows]

    def find_by_key(self, key: str) -> Optional[Assumption]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM assumptions WHERE key = ? ORDER BY created_at LIMIT 1", (key,)
            ).fetchone()
        return self._row_to_assumption(row) if row else None

    @staticmethod
    def _row_to_assumption(row: sqlite3.Row) -> Assumption:
        return Assumption(
            id=row["id"],
            key=row["key"],
            statement=row["statement"],
            check=_loads(row["check_json"], {}),
            status=row["status"],
            provenance=_loads(row["provenance_json"], {}),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_checked_at=row["last_checked_at"],
            invalidated_reason=row["invalidated_reason"],
            invalidated_at=row["invalidated_at"],
            check_count=row["check_count"],
            fail_count=row["fail_count"],
            seen_count=row["seen_count"],
            depends_on=_loads(row["depends_on_json"], []),
        )

    # --- evidence ---------------------------------------------------------------------
    def add_evidence(
        self,
        assumption: str,
        verdict: str,
        detail: str,
        observed: Optional[Dict[str, Any]] = None,
        source: str = "",
        created_at: Optional[float] = None,
    ) -> Evidence:
        created = time.time() if created_at is None else created_at
        ev = Evidence(
            id=evidence_id(assumption, verdict, created, source),
            assumption_id=assumption,
            verdict=verdict,
            detail=detail,
            observed=observed or {},
            source=source,
            created_at=created,
        )
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO evidence (id, assumption_id, verdict, detail,
                   observed_json, source, created_at) VALUES (?,?,?,?,?,?,?)""",
                (
                    ev.id, ev.assumption_id, ev.verdict, ev.detail,
                    _dumps(ev.observed), ev.source, ev.created_at,
                ),
            )
            self._conn.commit()
        return ev

    def evidence_for(self, asm_id: str, limit: int = 50) -> List[Evidence]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM evidence WHERE assumption_id = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (asm_id, limit),
            ).fetchall()
        return [
            Evidence(
                id=r["id"], assumption_id=r["assumption_id"], verdict=r["verdict"],
                detail=r["detail"], observed=_loads(r["observed_json"], {}),
                source=r["source"], created_at=r["created_at"],
            )
            for r in rows
        ]

    # --- decisions --------------------------------------------------------------------
    def upsert_decision(self, dec: Decision) -> Decision:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM decisions WHERE id = ?", (dec.id,)
            ).fetchone()
            if row is None:
                self._conn.execute(
                    """INSERT INTO decisions (id, key, statement, depends_on_json, state,
                       created_at, blocked_reason) VALUES (?,?,?,?,?,?,?)""",
                    (
                        dec.id, dec.key, dec.statement, _dumps(dec.depends_on),
                        dec.state, dec.created_at, dec.blocked_reason,
                    ),
                )
            else:
                existing = self._row_to_decision(row)
                existing.statement = dec.statement or existing.statement
                if dec.depends_on:
                    existing.depends_on = sorted(
                        set(existing.depends_on) | set(dec.depends_on)
                    )
                if dec.state != "open":
                    existing.state = dec.state
                    existing.blocked_reason = dec.blocked_reason
                self._conn.execute(
                    """UPDATE decisions SET statement=?, depends_on_json=?, state=?,
                       blocked_reason=? WHERE id=?""",
                    (
                        existing.statement, _dumps(existing.depends_on), existing.state,
                        existing.blocked_reason, existing.id,
                    ),
                )
            self._conn.commit()
        return self.get_decision(dec.id)  # type: ignore[return-value]

    def save_decision(self, dec: Decision) -> None:
        with self._lock:
            self._conn.execute(
                """UPDATE decisions SET statement=?, depends_on_json=?, state=?,
                   blocked_reason=? WHERE id=?""",
                (
                    dec.statement, _dumps(dec.depends_on), dec.state,
                    dec.blocked_reason, dec.id,
                ),
            )
            self._conn.commit()

    def get_decision(self, dec_id: str) -> Optional[Decision]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM decisions WHERE id = ?", (dec_id,)
            ).fetchone()
        return self._row_to_decision(row) if row else None

    def list_decisions(self) -> List[Decision]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM decisions ORDER BY created_at"
            ).fetchall()
        return [self._row_to_decision(r) for r in rows]

    def decisions_depending_on(self, asm_id: str) -> List[Decision]:
        return [d for d in self.list_decisions() if asm_id in d.depends_on]

    @staticmethod
    def _row_to_decision(row: sqlite3.Row) -> Decision:
        return Decision(
            id=row["id"], key=row["key"], statement=row["statement"],
            depends_on=_loads(row["depends_on_json"], []), state=row["state"],
            created_at=row["created_at"], blocked_reason=row["blocked_reason"],
        )


def _merge_provenance(existing: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    """Keep the original provenance, append each new origin once.

    The record's own first provenance *is* the first origin, so it is seeded here rather than
    waiting for a later merge to (wrongly) nominate the incoming origin as first.
    """
    merged = dict(existing or {})
    origins = list(merged.get("origins", []))
    first = merged.get("first_origin")
    if first is None and existing:
        first = {k: existing[k] for k in ("origin", "extractor", "recorded_at") if k in existing}
        merged["first_origin"] = first
        if first not in origins:
            origins.insert(0, first)
    if incoming:
        if not merged.get("first_origin"):
            merged["first_origin"] = incoming
        if incoming not in origins:
            origins.append(incoming)
    merged["origins"] = origins
    return merged
