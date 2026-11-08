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
from urllib.parse import urljoin

from .command_policy import is_allowed as _command_is_allowed, minimal_env as _minimal_env
from .model import ERROR, FAIL, PASS, CheckResult
from .url_safety import check_url as _check_url

# --- HTTP ------------------------------------------------------------------------------------
try:  # pragma: no cover - import guard
    import urllib.error
    import urllib.request
except Exception:  # pragma: no cover
    urllib = None  # type: ignore

# Rate-limit / transient statuses that mean "ask again later", not "assumption is false".
_TRANSIENT_HTTP = {408, 425, 429, 500, 502, 503, 504}


class CheckContext:
    """Everything a verifier may read. Injected so tests can control it exactly.

    Two policy inputs are read from here rather than from a check spec, so model input can never
    widen either one:

    * ``command_allowlist`` — the executables ``command_succeeds`` may run. Empty (the default)
      means command verification is disabled entirely.
    * ``allow_private_urls`` — whether HTTP checks may reach private/loopback/link-local
      destinations. Defaults to False; cloud-metadata endpoints stay blocked either way.
    """

    def __init__(
        self,
        root: Optional[str | Path] = None,
        cwd: Optional[str | Path] = None,
        http_get: Optional[Callable[[str, float], Any]] = None,
        runner: Optional[Callable[..., Any]] = None,
        env: Optional[Dict[str, str]] = None,
        default_timeout: float = 5.0,
        command_allowlist: Optional[tuple] = None,
        allow_private_urls: bool = False,
        url_resolver: Optional[Callable[[str], Any]] = None,
    ):
        self.root = Path(root) if root else None
        self.cwd = Path(cwd) if cwd else Path(os.getcwd())
        self._http_get = http_get
        self._runner = runner or subprocess.run
        self.env = env if env is not None else os.environ
        self.default_timeout = default_timeout
        self.command_allowlist = tuple(command_allowlist or ())
        self.allow_private_urls = bool(allow_private_urls)
        # Injectable so the SSRF tests never touch real DNS.
        self.url_resolver = url_resolver

    @property
    def runner(self) -> Callable[..., Any]:
        """The process runner used by the git and command verifiers (injectable for tests)."""
        return self._runner

    def resolve(self, path: str) -> Path:
        p = Path(path)
        if p.is_absolute() or self.root is None:
            return p
        return self.root / p

    def url_allowed(self, url: str) -> tuple:
        """Apply the SSRF policy to *url*. Returns ``(allowed, reason)``."""
        return _check_url(
            url,
            allow_private=self.allow_private_urls,
            resolver=self.url_resolver,
        )


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
    """Untracked files are reported but do not, by default, count as a dirty tree.

    A ledger database, a build artifact or a log file is not a change to what was committed.
    Counting it would invalidate "the tree is clean" for reasons unrelated to the assumption,
    so only tracked modifications invalidate unless the caller opts in with
    ``include_untracked: true``. The untracked list is always returned as observed evidence.
    """
    cwd = ctx.resolve(spec.get("cwd", "."))
    out, err = _run_git(ctx, ["status", "--porcelain"], cwd, ctx.default_timeout)
    if out is None:
        return _unknown(err or "git unavailable")
    lines = [line for line in out.splitlines() if line.strip()]
    untracked = [line[3:] for line in lines if line.startswith("??")]
    tracked = [line[3:] for line in lines if not line.startswith("??")]
    include_untracked = bool(spec.get("include_untracked", False))
    relevant = tracked + untracked if include_untracked else tracked
    if not relevant:
        return _ok("working tree clean", untracked=untracked[:50])
    return _bad("working tree has changes", changed=relevant[:50], untracked=untracked[:50])


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
    """Run an allowlisted probe command and use its exit code as the verdict.

    Disabled by default. Execution requires a non-empty ``command_allowlist`` on the context,
    which only plugin config can supply — a check spec cannot add to it. Refusals are ERROR
    (uncertainty), never FAIL: an unpermitted command is a policy outcome, not evidence that the
    assumption is false.
    """
    argv = spec.get("argv")
    allowed, reason = _command_is_allowed(argv, ctx.command_allowlist)
    if not allowed:
        return _unknown(f"command not permitted: {reason}", argv=argv[0] if isinstance(argv, list) and argv else None)
    cwd = ctx.resolve(spec.get("cwd", "."))
    timeout = float(spec.get("timeout", ctx.default_timeout))
    try:
        proc = ctx.runner(
            list(argv),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
            env=_minimal_env(),
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
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
_MAX_REDIRECTS = 5
_MAX_BODY_BYTES = 200_000


def _read_body(resp: Any, limit: int = _MAX_BODY_BYTES) -> str:
    return resp.read(limit).decode("utf-8", errors="replace")


def _build_no_redirect_opener() -> Any:
    """An opener that surfaces 3xx instead of following it, so each hop can be re-validated."""

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
            return None

    return urllib.request.build_opener(_NoRedirect())


_OPENER = _build_no_redirect_opener() if urllib is not None else None


def _fetch_once(url: str, timeout: float) -> Dict[str, Any]:
    """One request. Redirects are reported, never followed."""
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "assumption-ledger"})
    try:
        with _OPENER.open(req, timeout=timeout) as resp:  # noqa: S310
            status = getattr(resp, "status", 200)
            out: Dict[str, Any] = {"status": status, "body": _read_body(resp)}
            if status in _REDIRECT_CODES:
                location = (getattr(resp, "headers", None) or {}).get("Location")
                if location:
                    out["location"] = location
            return out
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = _read_body(exc)
        except Exception:
            pass
        out = {"status": exc.code, "body": body}
        if exc.code in _REDIRECT_CODES:
            location = (getattr(exc, "headers", None) or {}).get("Location")
            if location:
                out["location"] = location
        return out
    except urllib.error.URLError as exc:
        return {"error": str(exc.reason), "transient": True}
    except Exception as exc:  # socket timeout, ssl, dns ...
        return {"error": f"{type(exc).__name__}: {exc}", "transient": True}


def _fetch_following_safely(url: str, timeout: float, ctx: CheckContext) -> Dict[str, Any]:
    """Follow redirects manually, re-applying the SSRF policy to every hop.

    A redirect is a URL the model did not name, so an unvalidated hop would be a bypass. Each
    target is checked before it is requested; a refused hop ends the fetch as a policy denial.
    """
    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        resp = _fetch_once(current, timeout)
        if "error" in resp:
            return resp
        location = resp.get("location")
        if resp.get("status") not in _REDIRECT_CODES or not location:
            return resp
        target = urljoin(current, location)
        allowed, reason = ctx.url_allowed(target)
        if not allowed:
            return {"policy_denied": f"redirect target refused: {reason}"}
        current = target
    return {"error": "too many redirects", "transient": True}


def _http_get_guarded(url: str, timeout: float, ctx: CheckContext) -> Dict[str, Any]:
    """The single HTTP entry point for the verifiers.

    The SSRF policy is applied here, before any fetch, so it holds regardless of which transport
    is in use. An injected ``http_get`` (the test seam) is still subject to it — the policy
    belongs to the verifier, not to the transport.
    """
    allowed, reason = ctx.url_allowed(url)
    if not allowed:
        return {"policy_denied": reason}
    if ctx._http_get is not None:
        return ctx._http_get(url, timeout)
    return _fetch_following_safely(url, timeout, ctx)


def check_http_status(spec: Dict[str, Any], ctx: CheckContext) -> CheckResult:
    url = spec.get("url")
    if not url:
        return _unknown("url is required")
    timeout = float(spec.get("timeout", ctx.default_timeout))
    try:
        resp = _http_get_guarded(url, timeout, ctx)
    except Exception as exc:  # a raising getter is a provider failure, not a contradiction
        return _unknown(f"request failed: {type(exc).__name__}: {exc}", url=url)

    if resp is None:
        return _unknown("no response from provider", url=url)
    if "policy_denied" in resp:
        return _unknown(f"url refused by policy: {resp['policy_denied']}", url=url)
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
    try:
        resp = _http_get_guarded(url, timeout, ctx)
    except Exception as exc:
        return _unknown(f"request failed: {type(exc).__name__}: {exc}", url=url)
    if resp is None:
        return _unknown("provider unavailable", url=url)
    if "policy_denied" in resp:
        return _unknown(f"url refused by policy: {resp['policy_denied']}", url=url)
    if "error" in resp:
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
            return _ok("value matches", pointer=pointer, value=_summarize(actual))
        return _bad("value differs", pointer=pointer, actual=_summarize(actual))
    return _ok("pointer present", pointer=pointer, value=_summarize(actual))


def _summarize(value: Any, limit: int = 120) -> Any:
    """A bounded, type-tagged rendering of a response value for the evidence trail.

    Evidence is persisted and re-injected, so an endpoint must not be able to plant an arbitrary
    unbounded string there. Numbers, booleans and null pass through; everything else becomes a
    short repr with its type name, truncated at *limit*.
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:limit] if len(value) <= limit else value[:limit] + "…"
    if isinstance(value, (list, tuple)):
        return {"type": type(value).__name__, "len": len(value)}
    if isinstance(value, dict):
        return {"type": "dict", "keys": sorted(str(k) for k in value)[:20]}
    return {"type": type(value).__name__}


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


_WHERE_COLUMN = r'"?[A-Za-z_][A-Za-z0-9_]*"?'
_WHERE_ATOM = re.compile(
    r"""^\s*
    (?P<col>%s)\s*
    (?:
        (?P<op><=|>=|<>|!=|=|<|>)\s*(?P<val>\?|'(?:[^']|'')*'|-?\d+(?:\.\d+)?|NULL)\s*
      | (?P<isnot>IS\s+NOT\s+NULL)\s*
      | (?P<is>IS\s+NULL)\s*
    )
    $""" % _WHERE_COLUMN,
    re.IGNORECASE | re.VERBOSE,
)
_WHERE_JOIN = re.compile(r"\s+(AND|OR)\s+", re.IGNORECASE)


def _safe_where(clause: Any) -> Optional[str]:
    """Validate a caller-supplied WHERE clause, or return None to refuse it.

    The connection is read-only, but an unvalidated clause is still a SQL-injection surface, so
    only a narrow grammar is accepted: one or more ``column OP literal`` atoms joined by AND/OR,
    with ``OP`` in a fixed set and ``?`` for a bound parameter. Anything else — a subquery, a
    function call, a comment, a semicolon, an unbalanced quote — is refused, and the check returns
    ERROR (uncertainty) rather than running it.
    """
    if not isinstance(clause, str) or not clause.strip():
        return None
    if len(clause) > 500:
        return None
    if any(tok in clause for tok in (";", "--", "/*", "*/", "(", ")")):
        return None
    if re.search(r"\b(SELECT|UNION|INSERT|UPDATE|DELETE|DROP|ALTER|ATTACH|PRAGMA|EXEC|CAST)\b",
                 clause, re.IGNORECASE):
        return None
    atoms = _WHERE_JOIN.split(clause.strip())
    # atoms alternates: atom, JOIN, atom, JOIN, atom ...
    if not atoms or len(atoms) % 2 == 0:
        return None
    for index, part in enumerate(atoms):
        if index % 2 == 1:
            continue  # a join keyword, already matched by the split pattern
        if not _WHERE_ATOM.match(part):
            return None
    return clause.strip()


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
        safe = _safe_where(where)
        if safe is None:
            # A clause we cannot validate is uncertainty, never a verdict.
            return _unknown("where clause is not a supported simple predicate", table=table)
        sql += f" WHERE {safe}"
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
