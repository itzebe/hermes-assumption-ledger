"""Assumption Ledger — Hermes Agent plugin.

Captures operational assumptions from the assistant's own messages, persists them with
provenance, verifies them deterministically, and warns when work depends on one that evidence has
contradicted.

Design notes that matter for review:

* Everything is deterministic. No LLM call is made to extract or verify an assumption, so the
  plugin works with zero provider credentials and cannot be talked into a verdict.
* Uncertainty is never an invalidation. Provider errors, timeouts, rate limits and missing
  evidence produce UNVERIFIED/STALE, never INVALIDATED.
* Only the assistant's own messages are captured. Tool output (web pages, files, terminal
  listings) is never persisted as an assumption, so outside text cannot plant a durable record.
* Blocking is session-scoped and always escapable. A contradiction recorded in one session never
  stops another, and the affected session can inspect, re-check (``resolve``) or retire
  (``drop``) the assumption.
* Command execution is off by default and needs a user-configured allowlist. HTTP checks refuse
  loopback, private, link-local and cloud-metadata destinations unless the user opts in.
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

__version__ = "0.2.1"

_HOOKS = ("pre_llm_call", "post_llm_call", "pre_verify")
_TOOLSET = "assumption_ledger"

# Configuration defaults. Every value here is also declared in plugin.yaml, and each is read at
# runtime by ``_read_config`` below — there is no declared-but-unused setting.
DEFAULT_FRESHNESS_SECONDS = 24 * 60 * 60
DEFAULT_REINJECT_LIMIT = 25
MAX_REINJECT_LIMIT = 200
MIN_FRESHNESS_SECONDS = 1
MAX_FRESHNESS_SECONDS = 365 * 24 * 60 * 60


# --- configuration -----------------------------------------------------------------------------
def _plugin_config() -> Dict[str, Any]:
    """Read this plugin's settings from the active Hermes config scope.

    Uses ``PluginContext.get_config`` when a context is available (the sanctioned path, which
    reads ``plugins.entries.<id>.settings``). Falls back to an empty mapping outside Hermes, so a
    bare checkout gets the safe defaults rather than an error.
    """
    getter = _CONFIG_GETTER[0]
    if getter is None:
        return {}
    out: Dict[str, Any] = {}
    for key in ("freshness_seconds", "auto_check", "max_reinject", "command_allowlist",
                "allow_private_urls"):
        try:
            value = getter(key, None)
        except Exception:
            continue
        if value is not None:
            out[key] = value
    return out


def _coerce_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    """A bounded integer, or *default*. A bad value falls back rather than disabling the plugin."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes", "on"):
            return True
        if lowered in ("false", "0", "no", "off", ""):
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def resolve_config() -> Dict[str, Any]:
    """The effective, validated configuration. Exposed for tests and for ``/assumptions config``."""
    from .ledger.command_policy import normalize_allowlist

    raw = _plugin_config()
    return {
        "freshness_seconds": _coerce_int(
            raw.get("freshness_seconds"), DEFAULT_FRESHNESS_SECONDS,
            minimum=MIN_FRESHNESS_SECONDS, maximum=MAX_FRESHNESS_SECONDS,
        ),
        "auto_check": _coerce_bool(raw.get("auto_check"), False),
        "max_reinject": _coerce_int(
            raw.get("max_reinject"), DEFAULT_REINJECT_LIMIT,
            minimum=1, maximum=MAX_REINJECT_LIMIT,
        ),
        "command_allowlist": normalize_allowlist(raw.get("command_allowlist")),
        "allow_private_urls": _coerce_bool(raw.get("allow_private_urls"), False),
    }


# --- lazy engine -------------------------------------------------------------------------------
class LedgerHost:
    """Owns the single engine instance for this plugin and its storage location."""

    def __init__(self) -> None:
        self._engine = None
        self._error: Optional[str] = None
        self._config: Optional[Dict[str, Any]] = None

    @property
    def config(self) -> Dict[str, Any]:
        if self._config is None:
            try:
                self._config = resolve_config()
            except Exception:
                logger.exception("Assumption Ledger: config read failed, using defaults")
                self._config = {}
        return self._config

    def reset(self) -> None:
        """Drop cached state so a test (or a config change) is picked up on next use."""
        self._engine = None
        self._error = None
        self._config = None

    def engine(self):
        if self._engine is not None:
            return self._engine
        if self._error:
            return None
        try:
            from .ledger import CheckContext, LedgerEngine, LedgerStore
        except Exception as exc:  # pragma: no cover - only when the package is absent
            self._error = f"ledger package unavailable: {exc}"
            logger.warning("Assumption Ledger: %s", self._error)
            return None
        try:
            data_dir = _data_dir()
            store = LedgerStore(Path(data_dir) / "ledger.db")
            cfg = self.config
            self._engine = LedgerEngine(
                store,
                ctx=_check_context(cfg),
                freshness_seconds=cfg.get("freshness_seconds", DEFAULT_FRESHNESS_SECONDS),
                reinject_limit=cfg.get("max_reinject", DEFAULT_REINJECT_LIMIT),
                auto_check_on_capture=cfg.get("auto_check", False),
            )
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


def _check_context(cfg: Optional[Dict[str, Any]] = None):
    """Build the check context, applying the configured command and URL policies."""
    from .ledger import CheckContext

    cfg = cfg or {}
    try:
        from agent.runtime_cwd import resolve_agent_cwd  # type: ignore

        cwd = resolve_agent_cwd()
    except Exception:
        cwd = Path(os.getcwd())
    return CheckContext(
        root=Path(cwd),
        cwd=Path(cwd),
        command_allowlist=tuple(cfg.get("command_allowlist") or ()),
        allow_private_urls=bool(cfg.get("allow_private_urls", False)),
    )


# The PluginContext ``get_config`` callable, captured at registration. Kept as a one-element list
# so tests can install a stub without touching module globals by name.
_CONFIG_GETTER: List[Optional[Any]] = [None]

_HOST = LedgerHost()


# --- hook callbacks ----------------------------------------------------------------------------
def _session_of(kwargs: Dict[str, Any]) -> str:
    """The session id from a hook's kwargs, or '' when the host did not supply one."""
    value = kwargs.get("session_id")
    return str(value) if value else ""


def on_pre_llm_call(**kwargs: Any) -> Optional[Dict[str, Any]]:
    """Inject live assumptions and warn — but never veto — when work depends on a broken one.

    This hook does not return ``{"action": "block"}``. Blocking here would stop the very turn the
    agent needs to investigate and clear the problem, and it would stop it in every session,
    including ones that never touched the assumption. Instead the contradiction is delivered as
    context, with the commands that clear it. The hard gate lives in ``pre_verify``, which is
    scoped to the session that recorded the dependent decision and only holds a turn that is
    about to finish.
    """
    engine = _HOST.engine()
    if engine is None:
        return None
    try:
        session_id = _session_of(kwargs)
        blockers = engine.critical_blockers(session_id=session_id)
        parts: List[str] = []
        if blockers:
            lines = [
                "Assumption Ledger warning: work recorded in this session depends on an "
                "assumption that evidence has contradicted.",
            ]
            for b in blockers:
                asm = b["assumption"]
                lines.append(f"- INVALIDATED: {asm['statement']} (id {asm['id']})")
                for d in b["decisions"]:
                    lines.append(f"  relied on by: {d['statement']}")
            lines.append(
                "This is a warning, not a stop. To clear it: /assumptions show <id>, "
                "/assumptions resolve <id> (re-check), or /assumptions drop <id> (retire)."
            )
            parts.append("\n".join(lines))
        block = engine.render_reinjection(session_id=session_id)
        if block:
            parts.append(block)
        if parts:
            return {"context": "\n\n".join(parts)}
    except Exception:
        logger.exception("Assumption Ledger: pre_llm_call failed open")
    return None


def on_post_llm_call(**kwargs: Any) -> None:
    """Capture assumptions from the assistant's observable reply.

    Only the assistant's own text is captured. Tool output is untrusted and is deliberately never
    harvested (see ``on_post_tool_call``).
    """
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
                    "session_id": _session_of(kwargs),
                    "turn_id": kwargs.get("turn_id"),
                    "model": kwargs.get("model"),
                },
            )
    except Exception:
        logger.exception("Assumption Ledger: post_llm_call failed open")


def on_pre_verify(**kwargs: Any) -> Optional[Dict[str, Any]]:
    """At the edited-code verify gate, hold the turn while an assumption is contradicted.

    This is the one place the plugin can keep a turn going, and it is deliberately narrow: it
    fires only when a decision *recorded in this session* depends on an INVALIDATED assumption,
    and only at the verify gate rather than before inference. The agent has already had its turn
    to inspect and clear the block by this point.
    """
    engine = _HOST.engine()
    if engine is None:
        return None
    try:
        engine.sweep_stale()
        blockers = engine.critical_blockers(session_id=_session_of(kwargs))
        if not blockers:
            return None
        names = "; ".join(b["assumption"]["statement"][:60] for b in blockers)
        ids = ", ".join(b["assumption"]["id"] for b in blockers)
        return {
            "action": "continue",
            "message": (
                "Assumption Ledger: these assumptions are contradicted by evidence and are "
                f"relied on by planned work in this session: {names}. "
                f"Verify or revise them before finishing (/assumptions resolve {ids} to re-check, "
                f"or /assumptions drop {ids} to retire)."
            ),
        }
    except Exception:
        logger.exception("Assumption Ledger: pre_verify failed open")
        return None


def on_post_tool_call(**kwargs: Any) -> None:
    """Deliberately does nothing with tool output.

    Tool output is untrusted text: a web page, a file or a terminal listing can contain the
    literal string ``Assumption: ...``. Persisting that would write a durable record that is
    re-injected into every later turn, giving outside content a permanent place in the model's
    context. The hook is still registered so the manifest and the runtime agree, and so the
    behaviour is explicit rather than absent.

    Assumptions about a tool result belong in the assistant's own message, or in an explicit
    ``assumption_record`` call.
    """
    return None


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
        dec = engine.record_decision(
            statement,
            depends_on=[str(x) for x in depends_on],
            session_id=_session_of(kwargs),
        )
    except Exception as exc:
        return json.dumps({"error": f"could not record decision: {exc}"})
    return json.dumps({"id": dec.id, "state": dec.state, "blocked_reason": dec.blocked_reason})


def tool_assumption_resolve(args: Dict[str, Any], **kwargs: Any) -> str:
    """Re-check an assumption and report the outcome (does not force a status)."""
    engine, err = _engine_or_error()
    if err:
        return json.dumps(err)
    asm_id = (args.get("id") or "").strip()
    if not asm_id:
        return json.dumps({"error": "id is required"})
    try:
        return json.dumps(engine.resolve(asm_id, note=str(args.get("note") or "")))
    except Exception as exc:
        return json.dumps({"error": f"resolve failed: {exc}"})


def tool_assumption_drop(args: Dict[str, Any], **kwargs: Any) -> str:
    """Retire an assumption from the live set without claiming it was verified."""
    engine, err = _engine_or_error()
    if err:
        return json.dumps(err)
    asm_id = (args.get("id") or "").strip()
    if not asm_id:
        return json.dumps({"error": "id is required"})
    try:
        return json.dumps(engine.drop(asm_id, note=str(args.get("note") or "")))
    except Exception as exc:
        return json.dumps({"error": f"drop failed: {exc}"})


# --- slash command -----------------------------------------------------------------------------
_USAGE = (
    "Usage: /assumptions [status|list|check [id]|show <id>|resolve <id>|drop <id>|config]"
)


def command_assumptions(raw_args: str = "", **kwargs: Any) -> str:
    """`/assumptions [status|list|check [id]|show <id>|resolve <id>|drop <id>|config]`"""
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
        if arg == "config":
            cfg = _HOST.config
            lines = ["Assumption Ledger configuration (effective):"]
            for key in ("freshness_seconds", "auto_check", "max_reinject",
                        "command_allowlist", "allow_private_urls"):
                lines.append(f"  {key}: {cfg.get(key)}")
            if not cfg.get("command_allowlist"):
                lines.append("  command checks are disabled (command_allowlist is empty)")
            if not cfg.get("allow_private_urls"):
                lines.append("  HTTP checks refuse private/loopback/metadata destinations")
            return "\n".join(lines)
        if arg == "check":
            results = engine.verify_all()
            return f"Assumption Ledger: checked {len(results)} assumption(s).\n" + engine.render_status()
        parts = arg.split(maxsplit=1)
        sub = parts[0]
        rest = parts[1].strip() if len(parts) == 2 else ""
        if sub == "show" and rest:
            return engine.render_assumption(rest)
        if sub == "check" and rest:
            return json.dumps(engine.verify(rest), indent=2)
        if sub == "resolve" and rest:
            # "resolve <id> [note]" — the note is free text after the id.
            asm_id, _, note = rest.partition(" ")
            out = engine.resolve(asm_id, note=note.strip())
            if "error" in out:
                return f"Assumption Ledger: {out['error']}"
            return (
                f"Assumption Ledger: {out['assumption']} {out['outcome']} "
                f"({out['previous_status']} -> {out['status']}, verdict {out['verdict']})."
                + (f" Detail: {out['detail']}" if out.get("detail") else "")
            )
        if sub == "drop" and rest:
            asm_id, _, note = rest.partition(" ")
            out = engine.drop(asm_id, note=note.strip())
            if "error" in out:
                return f"Assumption Ledger: {out['error']}"
            extra = f" Unblocked {len(out['unblocked_decisions'])} decision(s)." if out["unblocked_decisions"] else ""
            return (
                f"Assumption Ledger: {out['assumption']} dropped "
                f"({out['previous_status']} -> DROPPED, retired without verification).{extra}"
            )
        return _USAGE
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
                "http_status, http_json_field, db_table_exists, db_row_count. "
                "command_succeeds is disabled unless the user configured command_allowlist; "
                "http_* refuse loopback, private, link-local and cloud-metadata hosts unless the "
                "user set allow_private_urls."
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

_RECOVER_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "Assumption id."},
        "note": {"type": "string", "description": "Optional note recorded with the action."},
    },
    "required": ["id"],
}


def register(ctx: Any) -> None:
    """Register hooks, tools and the slash command with Hermes."""
    # Capture the sanctioned config accessor so runtime settings are read from the active profile.
    getter = getattr(ctx, "get_config", None)
    if callable(getter):
        _CONFIG_GETTER[0] = getter
    _HOST.reset()

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
    ctx.register_tool(
        "assumption_resolve", _TOOLSET, _RECOVER_SCHEMA, tool_assumption_resolve,
        description="Re-check an assumption that is blocking work and report the outcome.",
    )
    ctx.register_tool(
        "assumption_drop", _TOOLSET, _RECOVER_SCHEMA, tool_assumption_drop,
        description="Retire an assumption from the live set without claiming it was verified.",
    )
    ctx.register_command(
        "assumptions", command_assumptions,
        description="Inspect, verify and clear operational assumptions.",
        args_hint="[status|list|check [id]|show <id>|resolve <id>|drop <id>|config]",
    )


_HOOK_CALLBACKS = {
    "pre_llm_call": on_pre_llm_call,
    "post_llm_call": on_post_llm_call,
    "pre_verify": on_pre_verify,
}
