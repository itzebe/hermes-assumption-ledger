"""Deterministic assumption extraction from observable agent output.

Input is text the agent produced (an assistant message) or text a tool produced (a result).
Nothing here inspects private model state; it only reads the strings Hermes already exposes.

The extractor is intentionally *conservative* and rule-based:

1. Explicit markers (``ASSUMPTION:``, ``ASSUME:``, ``PRECONDITION:``) are the highest-value
   signal and are parsed directly.
2. Natural-language patterns (``X exists``, ``the file Y contains Z``, ``on branch B``, ``the
   endpoint returns 200``) are recognised with anchored regular expressions.
3. Every extracted assumption carries the sentence it came from as provenance, so a human can
   audit the extraction rather than trust it.

A sentence that matches nothing produces nothing. Fabricating an assumption is worse than
missing one.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# --- explicit markers ------------------------------------------------------------------------
_MARKER_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:assumption|assume|precondition|assuming)\s*[:=]\s*(?P<body>.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")

# --- natural-language patterns ---------------------------------------------------------------
# Each entry: (name, compiled regex, builder(match) -> (statement, check_spec) | None)
_PATH = r"(?P<path>[~./][\w./\-]*[\w\-]|[\w\-][\w./\-]*\.[A-Za-z0-9]{1,8})"


def _file_exists_from(match: "re.Match[str]") -> Optional[tuple]:
    path = match.group("path")
    if not path or len(path) < 2:
        return None
    return (f"the file {path} exists", {"kind": "file_exists", "path": path})


def _file_contains_from(match: "re.Match[str]") -> Optional[tuple]:
    path = match.group("path")
    needle = (match.group("needle") or "").strip().strip("'\"`")
    if not path or not needle:
        return None
    return (
        f"the file {path} contains {needle!r}",
        {"kind": "file_contains", "path": path, "pattern": re.escape(needle)},
    )


def _git_branch_from(match: "re.Match[str]") -> Optional[tuple]:
    branch = (match.group("branch") or "").strip().strip("'\"`")
    if not branch:
        return None
    return (f"the repository is on branch {branch}", {"kind": "git_branch", "branch": branch})


def _git_commit_from(match: "re.Match[str]") -> Optional[tuple]:
    commit = (match.group("commit") or "").strip().strip("'\"`")
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", commit):
        return None
    return (
        f"HEAD is at commit {commit}",
        {"kind": "git_commit", "commit": commit.lower()},
    )


def _http_status_from(match: "re.Match[str]") -> Optional[tuple]:
    url = match.group("url").rstrip(".,)")
    code = match.group("code")
    try:
        code_i = int(code)
    except (TypeError, ValueError):
        return None
    if not (100 <= code_i <= 599):
        return None
    return (
        f"the endpoint {url} returns HTTP {code_i}",
        {"kind": "http_status", "url": url, "expected_status": code_i},
    )


def _http_reachable_from(match: "re.Match[str]") -> Optional[tuple]:
    url = match.group("url").rstrip(".,)")
    return (
        f"the endpoint {url} is reachable",
        {"kind": "http_status", "url": url, "expected_status": 200},
    )


def _table_rows_from(match: "re.Match[str]") -> Optional[tuple]:
    table = (match.group("table") or "").strip().strip("'\"`")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
        return None
    spec: Dict[str, Any] = {"kind": "db_row_count", "table": table}
    if match.group("count"):
        spec["equals"] = int(match.group("count"))
    else:
        spec["min"] = 1
    return (f"the table {table} has {'exactly ' + match.group('count') + ' rows' if match.group('count') else 'at least one row'}", spec)


def _env_set_from(match: "re.Match[str]") -> Optional[tuple]:
    name = (match.group("name") or "").strip().strip("'\"`")
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", name):
        return None
    return (
        f"the environment variable {name} is set",
        {"kind": "env_set", "name": name},
    )


_PATTERNS: List[tuple] = [
    (
        "file_contains",
        re.compile(
            r"\b(?:the\s+)?(?:file\s+)?(?P<path>[\w./\-]+\.[A-Za-z0-9]{1,8})\s+"
            r"(?:contains|includes)\s+(?P<needle>[^\s,;]+)",
            re.IGNORECASE,
        ),
        _file_contains_from,
    ),
    (
        "file_exists",
        re.compile(
            r"\b(?:the\s+)?(?:file|directory|folder|path)\s+(?P<path>[\w./\-]+\.[A-Za-z0-9]{1,8})\s+"
            r"(?:exists|is present|is available)\b",
            re.IGNORECASE,
        ),
        _file_exists_from,
    ),
    (
        "file_exists",
        re.compile(
            r"\b(?:the\s+)?(?P<path>[\w./\-]+\.[A-Za-z0-9]{1,8})\s+exists\b",
            re.IGNORECASE,
        ),
        _file_exists_from,
    ),
    (
        "git_branch",
        re.compile(
            r"\b(?:on|checked out to|currently on)\s+(?:the\s+)?branch\s+"
            r"(?P<branch>[\w./\-]+)",
            re.IGNORECASE,
        ),
        _git_branch_from,
    ),
    (
        "git_commit",
        re.compile(
            r"\b(?:HEAD\s+(?:is\s+)?(?:at\s+)?(?:commit\s+)?|commit\s+)"
            r"(?P<commit>[0-9a-fA-F]{7,40})\b",
            re.IGNORECASE,
        ),
        _git_commit_from,
    ),
    (
        "http_status",
        re.compile(
            r"\b(?P<url>https?://[^\s<>\"')\]]+?)\s+"
            r"(?:returns|responds with|responds)\s+(?:HTTP\s+)?(?P<code>[1-5]\d\d)\b",
            re.IGNORECASE,
        ),
        _http_status_from,
    ),
    (
        "http_reachable",
        re.compile(
            r"\b(?P<url>https?://[^\s<>\"')\]]+?)\s+"
            r"(?:is reachable|is live|is up|responds)\b",
            re.IGNORECASE,
        ),
        _http_reachable_from,
    ),
    (
        "db_row_count",
        re.compile(
            r"\b(?:the\s+)?table\s+(?P<table>[A-Za-z_][A-Za-z0-9_]*)\s+has\s+"
            r"(?:(?P<count>\d+)\s+rows?|at least one row)",
            re.IGNORECASE,
        ),
        _table_rows_from,
    ),
    (
        "env_set",
        re.compile(
            r"\b(?:the\s+)?environment variable\s+(?P<name>[A-Z][A-Z0-9_]{1,63})\s+is\s+set\b",
            re.IGNORECASE,
        ),
        _env_set_from,
    ),
]


@dataclass
class Extracted:
    statement: str
    check: Dict[str, Any]
    origin: str  # "marker" | pattern name
    source_text: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "statement": self.statement,
            "check": self.check,
            "origin": self.origin,
            "source_text": self.source_text,
        }


def extract_from_text(
    text: str,
    *,
    source: str = "message",
    max_items: int = 50,
    allow_markers: bool = True,
    allow_patterns: bool = True,
) -> List[Extracted]:
    """Extract assumptions from one block of observable text."""
    if not isinstance(text, str) or not text.strip():
        return []
    out: List[Extracted] = []
    seen: set = set()

    def add(statement: str, check: Dict[str, Any], origin: str, src: str) -> None:
        norm = re.sub(r"\s+", " ", statement.strip()).lower()
        if not norm or norm in seen:
            return
        seen.add(norm)
        out.append(Extracted(statement=statement.strip(), check=check, origin=origin, source_text=src))

    if allow_markers:
        for m in _MARKER_RE.finditer(text):
            body = (m.group("body") or "").strip()
            built = _build_from_body(body)
            if built is not None:
                statement, check = built
                add(statement, check, "marker", body)
            else:
                # An explicit marker with no checkable form is still recorded as an assumption
                # whose check is "unsupported", which stays UNVERIFIED rather than being lost.
                add(body, {"kind": "unsupported"}, "marker", body)

    if allow_patterns:
        for sentence in _SENTENCE_SPLIT.split(text):
            s = sentence.strip()
            if not s or len(s) > 400:
                continue
            for name, regex, builder in _PATTERNS:
                for m in regex.finditer(s):
                    try:
                        built = builder(m)
                    except Exception:
                        built = None
                    if built is None:
                        continue
                    statement, check = built
                    add(statement, check, name, s)
                    break  # one pattern hit per sentence is enough

    return out[:max_items]


def _build_from_body(body: str) -> Optional[tuple]:
    """Turn an explicit marker body into (statement, check) using the same patterns."""
    for _name, regex, builder in _PATTERNS:
        m = regex.search(body)
        if m:
            try:
                built = builder(m)
            except Exception:
                built = None
            if built is not None:
                return built
    return None


def extract_from_tool_result(
    tool_name: str, result: str, *, max_items: int = 20
) -> List[Extracted]:
    """Extract from a tool result.

    Tool output is where the *evidence* usually lives, so we only harvest explicit markers here.
    Scanning raw tool output for natural-language patterns would manufacture assumptions the
    agent never made (a directory listing mentioning "file.txt exists" is not an assumption).
    """
    return extract_from_text(
        result or "", source=f"tool:{tool_name}", max_items=max_items,
        allow_markers=True, allow_patterns=False,
    )
