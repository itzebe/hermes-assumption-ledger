"""Tests for the deterministic extractor.

The contract: extraction is conservative. A sentence that does not clearly state a checkable
assumption yields nothing, because a fabricated assumption is worse than a missed one.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ledger.extractor import extract_from_text, extract_from_tool_result  # noqa: E402


def _kinds(text):
    return {(e.check.get("kind"), e.statement) for e in extract_from_text(text)}


def test_explicit_marker_file_exists():
    out = extract_from_text("ASSUMPTION: config/settings.json exists")
    assert len(out) == 1
    assert out[0].check == {"kind": "file_exists", "path": "config/settings.json"}
    assert out[0].origin == "marker"


def test_marker_variants():
    for marker in ["ASSUMPTION:", "assume:", "PRECONDITION:", "Assuming:"]:
        out = extract_from_text(f"{marker} the file a.txt exists")
        assert out, f"{marker} should be recognised"
        assert out[0].check["kind"] == "file_exists"


def test_natural_language_patterns():
    text = (
        "The file requirements.txt contains requests. "
        "I am on branch main. "
        "HEAD is at commit 486f4a6a9918a4054373e33a200260ffd501645e. "
        "https://api.example.com/health returns 200. "
        "The table users has 5 rows. "
        "The environment variable API_KEY is set."
    )
    kinds = {e.check["kind"] for e in extract_from_text(text)}
    assert {"file_contains", "git_branch", "git_commit", "http_status", "db_row_count", "env_set"} <= kinds


def test_git_commit_requires_hex():
    # A short word after "commit" is not a commit hash and must not be extracted.
    out = extract_from_text("we should commit the changes")
    assert not any(e.check["kind"] == "git_commit" for e in out)


def test_http_status_range():
    assert any(e.check["kind"] == "http_status" for e in extract_from_text("http://x.test returns 200"))
    # 999 is not a valid status and must not be extracted.
    assert not any(e.check["kind"] == "http_status" for e in extract_from_text("http://x.test returns 999"))


def test_no_fabrication_on_prose():
    for prose in [
        "The tests pass.",
        "I think the deploy went fine.",
        "Everything looks good.",
        "The user wants a summary.",
    ]:
        assert extract_from_text(prose) == [], f"must not fabricate from: {prose}"


def test_dedupe_within_text():
    text = "ASSUMPTION: a.txt exists\nASSUMPTION: a.txt exists"
    out = extract_from_text(text)
    assert len(out) == 1


def test_marker_without_checkable_form_is_captured_unsupported():
    out = extract_from_text("ASSUMPTION: the system is probably healthy")
    assert len(out) == 1
    assert out[0].check["kind"] == "unsupported"


def test_tool_result_harvests_nothing_at_all():
    """Tool output is never an assumption source, not even for explicit markers.

    Raw tool prose must not manufacture assumptions, and a marker planted in outside text must not
    become a durable record either.
    """
    out = extract_from_tool_result("terminal", "total 4\ndrwxr-xr-x 2 user user 4096 file.txt exists here")
    assert out == []
    planted = extract_from_tool_result("terminal", "ASSUMPTION: out/report.csv exists")
    assert planted == [], "a marker in tool output must not be harvested"


def test_malformed_inputs_never_raise():
    for junk in ["", "   ", "\x00", "a" * 5000, "ASSUMPTION:", "commit ", "http://", "table  has rows"]:
        assert isinstance(extract_from_text(junk), list)


def test_provenance_source_text_recorded():
    out = extract_from_text("The file a.txt exists in the repo.")
    assert out and out[0].source_text


def test_max_items_bound():
    text = "\n".join(f"ASSUMPTION: f{i}.txt exists" for i in range(200))
    assert len(extract_from_text(text, max_items=10)) == 10
