"""Deterministic verifiers for operational assumptions.

Each verifier returns exactly one of three verdicts (``PASS``, ``FAIL``, ``ERROR``):

* ``PASS``  — the evidence positively supports the assumption.
* ``FAIL``  — the evidence definitively contradicts it.
* ``ERROR`` — we could not obtain evidence (missing target, timeout, rate limit, provider
  unavailable, unsupported environment). This must never be treated as a contradiction.

Verifiers are pure with respect to the ledger: they take a spec and a context, and return a
``CheckResult``. They perform no writes, so they are directly unit-testable.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .model import ERROR, FAIL, PASS, CheckResult

# --- HTTP ------------------------------------------------------------------------------------
try:  # pragma: no cover - import guard
    import urllib.error
    import urllib.request
except Exception:  # pragma: no cover
    urllib = None  # type: ignore

# Rate-limit / transient statuses that mean "ask again later", not "assumption is false".
_TRANSIENT_HTTP = {408, 425, 429, 500, 502, 503, 504}


class CheckContext:
    """Everything a verifier may read. Injected so tests can control it exactly."""

    def __init__(
        self,
        root: Optional[str | Path] = None,
        cwd: Optional[str | Path] = None,
        http_get: Optional[Callable[[str, float], Any]] = None,
        runner: Optional[Callable[..., Any]] = None,
        env: Optional[Dict[str, str]] = None,
        default_timeout: float = 5.0,
    ):
        self.root = Path(root) if root else None
        self.cwd = Path(cwd) if cwd else Path(os.getcwd())
        self._http_get = http_get
        self._runner = runner or subprocess.run
        self.env = env if env is not None else os.environ
        self.default_timeout = default_timeout

    @property
    def runner(self) -> Callable[..., Any]:
        """The process runner used by the git and command verifiers (injectable for tests)."""
        return self._runner

    def resolve(self, path: str) -> Path:
        p = Path(path)
        if p.is_absolute() or self.root is None:
            return p
        return self.root / p


def _ok(detail: str, **observed: Any) -> CheckResult:
    return CheckResult(verdict=PASS, detail=detail, observed=observed, source="verifier")


def _bad(detail: str, **observed: Any) -> CheckResult:
    return CheckResult(verdict=FAIL, detail=detail, observed=observed, source="verifier")


def _unknown(detail: str, **observed: Any) -> CheckResult:
    return CheckResult(verdict=ERROR, detail=detail, observed=observed, source="verifier")


# --- 1. filesystem ---------------------------------------------------------------------------
def check_file_exists(spec: Dict[str, Any], ctx: CheckContext) -> CheckResult:
    path = spec.get("path")
    if not path:
        return _unknown("no path supplied")
    target = ctx.resolve(path)
    try:
        exists = target.exists()
    except OSError as exc:
        return _unknown(f"filesystem error: {exc}", path=str(target))
    if exists:
        return _ok("path exists", path=str(target))
    return _bad("path does not exist", path=str(target))


def check_file_contains(spec: Dict[str, Any], ctx: CheckContext) -> CheckResult:
    path, pattern = spec.get("path"), spec.get("pattern")
    if not path or pattern is None:
        return _unknown("path and pattern are both required")
    target = ctx.resolve(path)
    if not target.exists():
        return _unknown("file not present, cannot evaluate contents", path=str(target))
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return _unknown(f"unreadable: {exc}", path=str(target))
    flags = re.MULTILINE
    if spec.get("ignore_case"):
        flags |= re.IGNORECASE
    try:
        found = re.search(pattern, text, flags) is not None
    except re.error as exc:
        return _unknown(f"invalid pattern: {exc}", pattern=pattern)
    if found:
        return _ok("pattern matched", path=str(target), pattern=pattern)
    return _bad("pattern not found", path=str(target), pattern=pattern)


def check_file_hash(spec: Dict[str, Any], ctx: CheckContext) -> CheckResult:
    import hashlib

    path = spec.get("path")
    expected = (spec.get("sha256") or "").lower()
    if not path or not expected:
        return _unknown("path and sha256 are both required")
    target = ctx.resolve(path)
    if not target.exists():
        return _unknown("file not present, cannot hash", path=str(target))
    algo = spec.get("algo", "sha256")
    try:
        h = hashlib.new(algo)
        with open(target, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
    except (OSError, ValueError) as exc:
        return _unknown(f"cannot hash: {exc}", path=str(target))
    actual = h.hexdigest()
    if actual.lower() == expected:
        return _ok("digest matches", path=str(target), sha256=actual)
    return _bad("digest differs", path=str(target), expected=expected, actual=actual)


# --- 2. git ----------------------------------------------------------------------------------
def _run_git(ctx: CheckContext, args: list, cwd: Path, timeout: float):
    try:
        proc = ctx.runner(
            ["git"] + args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return None, "git executable not found"
    except subprocess.TimeoutExpired:
        return None, "git command timed out"
    except OSError as exc:
        return None, f"git could not run: {exc}"
    if proc.returncode != 0:
        return None, (proc.stderr or "").strip() or f"git exited {proc.returncode}"
    return (proc.stdout or "").strip(), None


def check_git_branch(spec: Dict[str, Any], ctx: CheckContext) -> CheckResult:
    expected = spec.get("branch")
    if not expected:
        return _unknown("branch is required")
    cwd = ctx.resolve(spec.get("cwd", "."))
    out, err = _run_git(ctx, ["rev-parse", "--abbrev-ref", "HEAD"], cwd, ctx.default_timeout)
    if out is None:
        return _unknown(err or "git unavailable")
    if out == expected:
        return _ok("on expected branch", branch=out)
    return _bad("different branch checked out", expected=expected, actual=out)


def check_git_commit(spec: Dict[str, Any], ctx: CheckContext) -> CheckResult:
    expected = (spec.get("commit") or "").lower()
    if not expected:
        return _unknown("commit is required")
    cwd = ctx.resolve(spec.get("cwd", "."))
    out, err = _run_git(ctx, ["rev-parse", "HEAD"], cwd, ctx.default_timeout)
    if out is None:
        return _unknown(err or "git unavailable")
    actual = out.lower()
    if actual == expected or actual.startswith(expected):
        return _ok("HEAD matches", commit=actual)
    return _bad("HEAD differs", expected=expected, actual=actual)


def check_git_clean(spec: Dict[str, Any], ctx: CheckContext) -> CheckResult:
    cwd = ctx.resolve(spec.get("cwd", "."))
    out, err = _run_git(ctx, ["status", "--porcelain"], cwd, ctx.default_timeout)
    if out is None:
        return _unknown(err or "git unavailable")
    if not out:
        return _ok("working tree clean")
    files = [line[3:] for line in out.splitlines() if line.strip()]
    return _bad("working tree has changes", changed=files[:50])


# --- 3. environment / configuration ----------------------------------------------------------
def check_env_set(spec: Dict[str, Any], ctx: CheckContext) -> CheckResult:
    name = spec.get("name")
    if not name:
        return _unknown("name is required")
    value = ctx.env.get(name)
    if value is None or value == "":
        return _bad("environment variable is unset or empty", name=name)
    expected = spec.get("equals")
    if expected is not None and value != expected:
        # Do not echo the value: it may be a secret.
        return _bad("environment variable differs from expected", name=name)
    return _ok("environment variable is set", name=name)


def check_command_succeeds(spec: Dict[str, Any], ctx: CheckContext) -> CheckResult:
    """Run a read-only probe command and use its exit code as the verdict."""
    argv = spec.get("argv")
    if not isinstance(argv, list) or not argv:
        return _unknown("argv must be a non-empty list")
    if any(not isinstance(a, str) for a in argv):
        return _unknown("argv entries must be strings")
    cwd = ctx.resolve(spec.get("cwd", "."))
    timeout = float(spec.get("timeout", ctx.default_timeout))
    try:
        proc = ctx.runner(
            argv, cwd=str(cwd), capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError:
        return _unknown("command not found", argv=argv[0])
    except subprocess.TimeoutExpired:
        return _unknown("command timed out", argv=argv[0])
    except OSError as exc:
        return _unknown(f"command could not run: {exc}", argv=argv[0])
    if proc.returncode == 0:
        return _ok("command exited 0", returncode=0)
    return _bad("command exited non-zero", returncode=proc.returncode)


# --- 4. HTTP ---------------------------------------------------------------------------------
def _default_http_get(url: str, timeout: float) -> Dict[str, Any]:
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "assumption-ledger"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            body = resp.read(200_000).decode("utf-8", errors="replace")
            return {"status": getattr(resp, "status", 200), "body": body}
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read(200_000).decode("utf-8", errors="replace")
        except Exception:
            pass
        return {"status": exc.code, "body": body}
    except urllib.error.URLError as exc:
        return {"error": str(exc.reason), "transient": True}
    except Exception as exc:  # socket timeout, ssl, dns ...
        return {"error": f"{type(exc).__name__}: {exc}", "transient": True}


def check_http_status(spec: Dict[str, Any], ctx: CheckContext) -> CheckResult:
    url = spec.get("url")
    if not url:
        return _unknown("url is required")
    timeout = float(spec.get("timeout", ctx.default_timeout))
    getter = ctx._http_get or _default_http_get
    try:
        resp = getter(url, timeout)
    except Exception as exc:  # a raising getter is a provider failure, not a contradiction
        return _unknown(f"request failed: {type(exc).__name__}: {exc}", url=url)

    if resp is None:
        return _unknown("no response from provider", url=url)
    if "error" in resp:
        return _unknown(f"provider unavailable: {resp['error']}", url=url, transient=True)

    status = int(resp.get("status", 0))
    expected = spec.get("expected_status", 200)
    expected_set = set(expected) if isinstance(expected, (list, tuple)) else {expected}

    if status in _TRANSIENT_HTTP and status not in expected_set:
        # A 5xx / 429 is the provider saying "not now", never proof the assumption is false.
        return _unknown(f"transient HTTP {status}", url=url, status=status, transient=True)
    if status in expected_set:
        return _ok(f"HTTP {status}", url=url, status=status)
    return _bad(f"HTTP {status} not in {sorted(expected_set)}", url=url, status=status)


def check_http_json_field(spec: Dict[str, Any], ctx: CheckContext) -> CheckResult:
    url, pointer = spec.get("url"), spec.get("pointer")
    if not url or pointer is None:
        return _unknown("url and pointer are both required")
    timeout = float(spec.get("timeout", ctx.default_timeout))
    getter = ctx._http_get or _default_http_get
    try:
        resp = getter(url, timeout)
    except Exception as exc:
        return _unknown(f"request failed: {type(exc).__name__}: {exc}", url=url)
    if resp is None or "error" in resp:
        return _unknown("provider unavailable", url=url)
    status = int(resp.get("status", 0))
    if status in _TRANSIENT_HTTP:
        return _unknown(f"transient HTTP {status}", url=url, status=status)
    if status >= 400:
        return _bad(f"HTTP {status}", url=url, status=status)
    try:
        payload = json.loads(resp.get("body") or "null")
    except ValueError:
        return _unknown("response body is not JSON", url=url)
    actual = _resolve_pointer(payload, pointer)
    if actual is _MISSING:
        return _bad("pointer not present in payload", url=url, pointer=pointer)
    if "equals" in spec:
        if actual == spec["equals"]:
            return _ok("value matches", pointer=pointer, value=actual)
        return _bad("value differs", pointer=pointer, actual=actual)
    return _ok("pointer present", pointer=pointer, value=actual)


_MISSING = object()


def _resolve_pointer(payload: Any, pointer: str) -> Any:
    """Resolve a dotted path (``a.b.0.c``) against a decoded JSON payload."""
    cur = payload
    for part in str(pointer).split("."):
        if part == "":
            continue
        if isinstance(cur, dict):
            if part not in cur:
                return _MISSING
            cur = cur[part]
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return _MISSING
        else:
            return _MISSING
    return cur


# --- 5. local database -----------------------------------------------------------------------
def check_db_table_exists(spec: Dict[str, Any], ctx: CheckContext) -> CheckResult:
    path, table = spec.get("path"), spec.get("table")
    if not path or not table:
        return _unknown("path and table are both required")
    db = ctx.resolve(path)
    if not db.exists():
        return _unknown("database file not present", path=str(db))
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return _unknown(f"cannot open database read-only: {exc}", path=str(db))
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    except sqlite3.Error as exc:
        return _unknown(f"query failed: {exc}", path=str(db))
    finally:
        conn.close()
    if row:
        return _ok("table exists", table=table)
    return _bad("table missing", table=table)


def check_db_row_count(spec: Dict[str, Any], ctx: CheckContext) -> CheckResult:
    path, table = spec.get("path"), spec.get("table")
    if not path or not table:
        return _unknown("path and table are both required")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
        return _unknown("unsafe table name", table=table)
    db = ctx.resolve(path)
    if not db.exists():
        return _unknown("database file not present", path=str(db))
    where = spec.get("where")
    sql = f'SELECT COUNT(*) FROM "{table}"'
    params: tuple = ()
    if where:
        sql += f" WHERE {where}"  # caller-supplied predicate, read-only connection
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return _unknown(f"cannot open database read-only: {exc}", path=str(db))
    try:
        actual = conn.execute(sql, params).fetchone()[0]
    except sqlite3.Error as exc:
        return _unknown(f"query failed: {exc}", table=table)
    finally:
        conn.close()

    observed = {"table": table, "count": actual}
    if "equals" in spec:
        if actual == spec["equals"]:
            return _ok("row count equals expected", **observed)
        return _bad("row count differs", expected=spec["equals"], **observed)
    if "min" in spec and actual < spec["min"]:
        return _bad("row count below minimum", min=spec["min"], **observed)
    if "max" in spec and actual > spec["max"]:
        return _bad("row count above maximum", max=spec["max"], **observed)
    return _ok("row count within range", **observed)


# --- registry --------------------------------------------------------------------------------
VERIFIERS: Dict[str, Callable[[Dict[str, Any], CheckContext], CheckResult]] = {
    "file_exists": check_file_exists,
    "file_contains": check_file_contains,
    "file_hash": check_file_hash,
    "git_branch": check_git_branch,
    "git_commit": check_git_commit,
    "git_clean": check_git_clean,
    "env_set": check_env_set,
    "command_succeeds": check_command_succeeds,
    "http_status": check_http_status,
    "http_json_field": check_http_json_field,
    "db_table_exists": check_db_table_exists,
    "db_row_count": check_db_row_count,
}

SUPPORTED_KINDS = tuple(sorted(VERIFIERS))


def run_check(
    spec: Dict[str, Any], ctx: Optional[CheckContext] = None
) -> CheckResult:
    """Dispatch a check spec to its verifier.

    An unknown kind or a malformed spec yields ``ERROR`` (UNVERIFIED), never a fabricated
    pass or fail.
    """
    ctx = ctx or CheckContext()
    if not isinstance(spec, dict):
        return _unknown("check spec must be a mapping")
    kind = spec.get("kind")
    verifier = VERIFIERS.get(str(kind or ""))
    if verifier is None:
        return _unknown(f"unsupported check kind {kind!r}", supported=list(SUPPORTED_KINDS))
    try:
        result = verifier(spec, ctx)
    except Exception as exc:  # a verifier bug is uncertainty, never a contradiction
        return _unknown(f"verifier raised {type(exc).__name__}: {exc}", kind=kind)
    if not isinstance(result, CheckResult):
        return _unknown("verifier returned an invalid result", kind=kind)
    if result.verdict not in (PASS, FAIL, ERROR):
        return _unknown(f"verifier returned unknown verdict {result.verdict!r}", kind=kind)
    if not result.source:
        result.source = f"verifier:{kind}"
    return result
