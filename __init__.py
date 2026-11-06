"""Assumption Ledger — Hermes Agent plugin.

Captures operational assumptions from observable agent activity, persists them with provenance,
verifies them deterministically, and blocks dependent decisions when one is contradicted.

Design notes that matter for review:

* Everything is deterministic. No LLM call is made to extract or verify an assumption, so the
  plugin works with zero provider credentials and cannot be talked into a verdict.
* Uncertainty is never an invalidation. Provider errors, timeouts, rate limits and missing
  evidence produce UNVERIFIED/STALE, never INVALIDATED.
* The engine never blocks unrelated work: only a decision that explicitly depends on an
  invalidated assumption is marked blocked, and the pre-verify gate only keeps the turn going
  when such a dependency exists.
* Storage goes through the sanctioned ``plugins.plugin_storage`` seam, so records survive
  ``hermes plugins update`` and ``hermes plugins remove``.

The plugin degrades honestly when a Hermes import is unavailable (for example in a bare unit
test): the engine still loads, and the integration simply does not register.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

__version__ = "0.1.0"

_HOOKS = ("pre_llm_call", "post_llm_call", "pre_verify")
_TOOLSET = "assumption_ledger"


# --- lazy engine -------------------------------------------------------------------------------
class LedgerHost:
    """Owns the single engine instance for this plugin and its storage location."""

    def __init__(self) -> None:
        self._engine = None
        self._error: Optional[str] = None

    def engine(self):
        if self._engine is not None:
            return self._engine
        if self._error:
            return None
        try:
            from ledger import CheckContext, LedgerEngine, LedgerStore
        except Exception as exc:  # pragma: no cover - only when the package is absent
            self._error = f"ledger package unavailable: {exc}"
            logger.warning("Assumption Ledger: %s", self._error)
            return None
        try:
            data_dir = _data_dir()
            store = LedgerStore(Path(data_dir) / "ledger.db")
            self._engine = LedgerEngine(store, ctx=_check_context())
        except Exception as exc:  # pragma: no cover
            self._error = f"could not open ledger store: {exc}"
            logger.exception("Assumption Ledger: %s", self._error)
            return None
        return self._engine

    @property
    def error(self) -> Optional[str]:
        return self._error


def _data_dir() -> Path:
    """Resolve the sanctioned per-plugin data directory.

    Prefers Hermes' ``plugins.plugin_storage.plugin_data_dir`` so the data follows the active
    profile and survives updates. Falls back to ``HERMES_HOME`` (or a local dir) only when the
    Hermes runtime is not importable, which keeps the plugin usable in a bare checkout.
    """
    try:
        from plugins.plugin_storage import plugin_data_dir  # type: ignore

        return Path(plugin_data_dir("assumption-ledger"))
    except Exception:
        pass
    home = os.environ.get("HERMES_HOME")
    base = Path(home) if home else Path.home() / ".hermes"
    return base / "plugin-data" / "assumption-ledger"


def _check_context():
    from ledger import CheckContext

    try:
        from agent.runtime_cwd import resolve_agent_cwd  # type: ignore

        cwd = resolve_agent_cwd()
    except Exception:
        cwd = Path(os.getcwd())
    return CheckContext(root=Path(cwd), cwd=Path(cwd))


_HOST = LedgerHost()


# --- hook callbacks ----------------------------------------------------------------------------
def on_pre_llm_call(**kwargs: Any) -> Optional[Dict[str, Any]]:
    """Re-inject live assumptions, and stop the turn if a plan is built on a broken one.

    Returning ``{"action": "block", "message": ...}`` ends the turn before inference. We only do
    that when an assumption is INVALIDATED *and* a recorded decision depends on it, so unrelated
    work is never blocked.
    """
    engine = _HOST.engine()
    if engine is None:
        return None
    try:
        blockers = engine.critical_blockers()
        if blockers:
            lines = ["Assumption Ledger stopped this turn: a decision depends on an assumption "
                     "that evidence has contradicted."]
            for b in blockers:
                asm = b["assumption"]
                lines.append(f"- INVALIDATED: {asm['statement']} (reason: {asm['invalidated_reason']})")
                for d in b["decisions"]:
                    lines.append(f"  blocks decision: {d['statement']}")
            lines.append("Re-check the assumption or change the plan before continuing.")
            return {"action": "block", "message": "\n".join(lines)}
        block = engine.render_reinjection()
        if block:
            return {"context": block}
    except Exception:
        logger.exception("Assumption Ledger: pre_llm_call failed open")
    return None


def on_post_llm_call(**kwargs: Any) -> None:
    """Capture assumptions from the assistant's observable reply."""
    engine = _HOST.engine()
    if engine is None:
        return
    try:
        text = kwargs.get("assistant_response") or ""
        if text:
            engine.observe_message(
                text,
                origin="assistant",
                meta={
                    "session_id": kwargs.get("session_id"),
                    "turn_id": kwargs.get("turn_id"),
                    "model": kwargs.get("model"),
                },
            )
    except Exception:
        logger.exception("Assumption Ledger: post_llm_call failed open")


def on_pre_verify(**kwargs: Any) -> Optional[Dict[str, Any]]:
    """At the edited-code verify gate, hold the turn while an assumption is contradicted.

    Returns a ``continue`` directive only when there is a real blocker, so the gate stays inert
    for ordinary turns.
    """
    engine = _HOST.engine()
    if engine is None:
        return None
    try:
        engine.sweep_stale()
        blockers = engine.critical_blockers()
        if not blockers:
            return None
        names = "; ".join(b["assumption"]["statement"][:60] for b in blockers)
        return {
            "action": "continue",
            "message": (
                "Assumption Ledger: these assumptions are contradicted by evidence and are "
                f"relied on by planned work: {names}. Verify or revise them before finishing."
            ),
        }
    except Exception:
        logger.exception("Assumption Ledger: pre_verify failed open")
        return None


def on_post_tool_call(**kwargs: Any) -> None:
    """Capture explicit assumptions that a tool result surfaced (e.g. a probe script)."""
    engine = _HOST.engine()
    if engine is None:
        return
    try:
        result = kwargs.get("result")
        if isinstance(result, dict):
            result = json.dumps(result, default=str)
        if not result:
            return
        engine.observe_tool_result(
            str(kwargs.get("tool_name") or "tool"),
            str(result)[:20000],
            meta={"tool_call_id": kwargs.get("tool_call_id"), "session_id": kwargs.get("session_id")},
        )
    except Exception:
        logger.exception("Assumption Ledger: post_tool_call failed open")


# --- tool handlers -----------------------------------------------------------------------------
def _engine_or_error():
    engine = _HOST.engine()
    if engine is None:
        return None, {"error": _HOST.error or "ledger engine unavailable"}
    return engine, None


def tool_assumption_record(args: Dict[str, Any], **kwargs: Any) -> str:
    """Record an explicit assumption with a deterministic check."""
    engine, err = _engine_or_error()
    if err:
        return json.dumps(err)
    statement = (args.get("statement") or "").strip()
    check = args.get("check")
    if not statement:
        return json.dumps({"error": "statement is required"})
    if not isinstance(check, dict) or not check.get("kind"):
        return json.dumps({"error": "check must be a mapping with a 'kind'"})
    try:
        asm = engine.add_assumption(
            statement, check, origin="tool",
            meta={"session_id": kwargs.get("session_id")},
        )
    except Exception as exc:
        return json.dumps({"error": f"could not record: {exc}"})
    return json.dumps({"id": asm.id, "status": asm.status, "statement": asm.statement})


def tool_assumption_check(args: Dict[str, Any], **kwargs: Any) -> str:
    """Verify one assumption, or every assumption, and return the result."""
    engine, err = _engine_or_error()
    if err:
        return json.dumps(err)
    asm_id = args.get("id")
    try:
        if asm_id:
            return json.dumps(engine.verify(asm_id))
        return json.dumps({"results": engine.verify_all()})
    except Exception as exc:
        return json.dumps({"error": f"check failed: {exc}"})


def tool_assumption_status(args: Dict[str, Any], **kwargs: Any) -> str:
    """Return the ledger summary (status counts, invalidated, blockers)."""
    engine, err = _engine_or_error()
    if err:
        return json.dumps(err)
    try:
        return json.dumps(engine.summary())
    except Exception as exc:
        return json.dumps({"error": f"status failed: {exc}"})


def tool_assumption_decide(args: Dict[str, Any], **kwargs: Any) -> str:
    """Record a decision and the assumptions it depends on."""
    engine, err = _engine_or_error()
    if err:
        return json.dumps(err)
    statement = (args.get("statement") or "").strip()
    depends_on = args.get("depends_on") or []
    if not statement:
        return json.dumps({"error": "statement is required"})
    if not isinstance(depends_on, list):
        return json.dumps({"error": "depends_on must be a list of assumption ids"})
    try:
        dec = engine.record_decision(statement, depends_on=[str(x) for x in depends_on])
    except Exception as exc:
        return json.dumps({"error": f"could not record decision: {exc}"})
    return json.dumps({"id": dec.id, "state": dec.state, "blocked_reason": dec.blocked_reason})


# --- slash command -----------------------------------------------------------------------------
def command_assumptions(raw_args: str = "", **kwargs: Any) -> str:
    """`/assumptions [status|list|check <id>|show <id>]`"""
    engine, err = _engine_or_error()
    if err:
        return err
    arg = (raw_args or "").strip()
    try:
        if not arg or arg in ("status", "summary"):
            return engine.render_status()
        if arg == "list":
            rows = engine.store.list_assumptions()
            if not rows:
                return "Assumption Ledger: no assumptions captured yet."
            return "\n".join(f"[{a.status}] {a.id} {a.statement[:80]}" for a in rows)
        if arg == "check":
            results = engine.verify_all()
            return f"Assumption Ledger: checked {len(results)} assumption(s).\n" + engine.render_status()
        parts = arg.split(maxsplit=1)
        if parts[0] == "show" and len(parts) == 2:
            return engine.render_assumption(parts[1].strip())
        if parts[0] == "check" and len(parts) == 2:
            res = engine.verify(parts[1].strip())
            return json.dumps(res, indent=2)
        return "Usage: /assumptions [status|list|check [id]|show <id>]"
    except Exception as exc:
        return f"Assumption Ledger error: {exc}"


# --- registration ------------------------------------------------------------------------------
_RECORD_SCHEMA = {
    "type": "object",
    "properties": {
        "statement": {"type": "string", "description": "The assumption in plain language."},
        "check": {
            "type": "object",
            "description": (
                "Deterministic check spec. Supported kinds: file_exists, file_contains, "
                "file_hash, git_branch, git_commit, git_clean, env_set, command_succeeds, "
                "http_status, http_json_field, db_table_exists, db_row_count."
            ),
        },
    },
    "required": ["statement", "check"],
}

_STATUS_SCHEMA = {"type": "object", "properties": {}, "required": []}

_CHECK_SCHEMA = {
    "type": "object",
    "properties": {"id": {"type": "string", "description": "Assumption id; omit to check all."}},
    "required": [],
}

_DECIDE_SCHEMA = {
    "type": "object",
    "properties": {
        "statement": {"type": "string", "description": "The plan step or action."},
        "depends_on": {
            "type": "array", "items": {"type": "string"},
            "description": "Assumption ids this decision relies on.",
        },
    },
    "required": ["statement"],
}


def register(ctx: Any) -> None:
    """Register hooks, tools and the slash command with Hermes."""
    for hook in _HOOKS:
        ctx.register_hook(hook, _HOOK_CALLBACKS[hook])
    ctx.register_hook("post_tool_call", on_post_tool_call)

    ctx.register_tool(
        "assumption_record", _TOOLSET, _RECORD_SCHEMA, tool_assumption_record,
        description="Record an operational assumption with a deterministic check.",
    )
    ctx.register_tool(
        "assumption_check", _TOOLSET, _CHECK_SCHEMA, tool_assumption_check,
        description="Verify recorded assumptions and update their status.",
    )
    ctx.register_tool(
        "assumption_status", _TOOLSET, _STATUS_SCHEMA, tool_assumption_status,
        description="Show the assumption ledger summary.",
    )
    ctx.register_tool(
        "assumption_decide", _TOOLSET, _DECIDE_SCHEMA, tool_assumption_decide,
        description="Record a decision and the assumptions it depends on.",
    )
    ctx.register_command(
        "assumptions", command_assumptions,
        description="Inspect and verify operational assumptions.",
        args_hint="[status|list|check [id]|show <id>]",
    )


_HOOK_CALLBACKS = {
    "pre_llm_call": on_pre_llm_call,
    "post_llm_call": on_post_llm_call,
    "pre_verify": on_pre_verify,
}
