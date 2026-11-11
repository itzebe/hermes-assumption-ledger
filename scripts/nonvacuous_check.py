#!/usr/bin/env python3
"""Non-vacuity harness: revert one security fix at a time and record which tests fail.

A test that passes both with and without its fix proves nothing. This script mutates the source,
runs the focused suites, restores the file, and prints the result. It never leaves the tree
modified.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

REVERTS = [
    (
        "R1 command: allow execution with no allowlist",
        "ledger/command_policy.py",
        '''    if not allowlist:
        return False, "command checks are disabled (no command_allowlist configured)"''',
        '''    if not allowlist:
        pass  # REVERTED''',
        ["tests/test_command_policy.py", "tests/test_verifiers.py", "tests/test_config.py"],
    ),
    (
        "R2 command: inherit the full host environment",
        "ledger/command_policy.py",
        '''    return {
        "PATH": path or os.environ.get("PATH") or "/usr/local/bin:/usr/bin:/bin",
        "HOME": "",
        "TMPDIR": "",
        "LC_ALL": "C",
        "LANG": "C",
    }''',
        '''    import os as _os
    return dict(_os.environ)  # REVERTED''',
        ["tests/test_command_policy.py", "tests/test_verifiers.py"],
    ),
    (
        "R3 command: reduce argv[0] to its basename again",
        "ledger/command_policy.py",
        '''    name = first.strip()
    if not name or "/" in name or "\\\\" in name:
        return None
    return name''',
        '''    name = first.strip()
    if "/" in name or "\\\\" in name:
        name = PurePosixPath(PureWindowsPath(name).name).name
    return name or None''',
        ["tests/test_command_policy.py", "tests/test_verifiers.py"],
    ),
    (
        "R3b command: execute the model-supplied argv[0] verbatim",
        "ledger/command_policy.py",
        '''    return [resolved, *list(argv)[1:]], "ok"''',
        '''    return list(argv), "ok"  # REVERTED: run the model string''',
        ["tests/test_command_policy.py", "tests/test_verifiers.py"],
    ),
    (
        "R3c command: skip executable resolution (accept any allowlisted name)",
        "ledger/command_policy.py",
        '''    if resolve_executable(name) is None:
        return False, f"executable {name!r} could not be resolved on PATH"
    return True, "ok"''',
        '''    return True, "ok"  # REVERTED: no resolution''',
        ["tests/test_command_policy.py", "tests/test_verifiers.py"],
    ),
    (
        "R4 SSRF: allow private/loopback/link-local",
        "ledger/url_safety.py",
        '''    if is_restricted_ip(ip) and not allow_private:
        return False, "private, loopback, link-local or reserved address"''',
        '''    if False:  # REVERTED: private not blocked
        return False, "private, loopback, link-local or reserved address"''',
        ["tests/test_ssrf.py"],
    ),
    (
        "R5 SSRF: allow cloud metadata endpoints",
        "ledger/url_safety.py",
        '''    if is_always_blocked_ip(ip):
        return False, "cloud metadata address"''',
        '''    if False:  # REVERTED: metadata not blocked
        return False, "cloud metadata address"''',
        ["tests/test_ssrf.py"],
    ),
    (
        "R6 SSRF: accept any scheme",
        "ledger/url_safety.py",
        '''    if parts.scheme.lower() not in HTTP_SCHEMES:
        return f"unsupported scheme {parts.scheme or '<empty>'!r}"''',
        '''    if False:  # REVERTED: any scheme
        return f"unsupported scheme {parts.scheme or '<empty>'!r}"''',
        ["tests/test_ssrf.py"],
    ),
    (
        "R7 SSRF: stop re-validating redirect targets",
        "ledger/verifiers.py",
        '''        allowed, reason = ctx.url_allowed(target)
        if not allowed:
            return {"policy_denied": f"redirect target refused: {reason}"}''',
        '''        pass  # REVERTED: redirect target not validated''',
        ["tests/test_ssrf.py"],
    ),
    (
        "R8 SSRF: skip the policy in the verifier entry point",
        "ledger/verifiers.py",
        '''    allowed, reason = ctx.url_allowed(url)
    if not allowed:
        return {"policy_denied": reason}''',
        '''    pass  # REVERTED: no policy''',
        ["tests/test_ssrf.py"],
    ),
    (
        "R9 capture: persist assumptions from tool output again",
        "ledger/extractor.py",
        '''    Assumptions about a tool result belong in the assistant's own message, or in an explicit
    ``assumption_record`` call.
    """
    return []''',
        '''    Assumptions about a tool result belong in the assistant's own message, or in an explicit
    ``assumption_record`` call.
    """
    return extract_from_text(
        result or "", source=f"tool:{tool_name}", max_items=max_items,
        allow_markers=True, allow_patterns=False,
    )  # REVERTED''',
        ["tests/test_injection.py", "tests/test_extractor.py", "tests/test_plugin_integration.py"],
    ),
    (
        "R10 blocking: veto every turn again (pre_llm_call block)",
        "__init__.py",
        '''        if parts:
            return {"context": "\\n\\n".join(parts)}''',
        '''        if blockers:
            return {"action": "block", "message": "REVERTED"}
        if parts:
            return {"context": "\\n\\n".join(parts)}''',
        ["tests/test_plugin_integration.py"],
    ),
    (
        "R11 blocking: ignore the session scope",
        "ledger/engine.py",
        '''            if session_id:
                decs = [d for d in decs if d.session_id == session_id]''',
        '''            pass  # REVERTED: not session-scoped''',
        ["tests/test_recovery.py", "tests/test_plugin_integration.py"],
    ),
    (
        "R12 drop: fabricate a PASS instead of recording a retirement",
        "ledger/engine.py",
        '''        self.store.add_evidence(
            asm.id,
            DROPPED_EVIDENCE,
            f"retired by user without verification{(': ' + note) if note else ''}",
            source="engine:drop",
        )''',
        '''        self.store.add_evidence(asm.id, "pass", "REVERTED", source="engine:drop")''',
        ["tests/test_recovery.py"],
    ),
    (
        "R13 resolve: let the caller force a status",
        "ledger/engine.py",
        '''        before = asm.status
        result = self.verify(asm_id)
        after = result.get("status", before)''',
        '''        before = asm.status
        result = self.verify(asm_id)
        after = result.get("status", before)
        if note == "force":  # REVERTED: an override path
            asm.status = VALID
            self.store.save_assumption(asm)
            after = VALID''',
        ["tests/test_recovery.py"],
    ),
    (
        "R14 invariant: let ERROR soften a contradiction",
        "ledger/model.py",
        '''    if previous_status in (INVALIDATED, DROPPED):
        return previous_status''',
        '''    if False:  # REVERTED
        return previous_status''',
        ["tests/test_recovery.py", "tests/test_frozen_oracles.py"],
    ),
    (
        "R15 config: ignore max_reinject",
        "ledger/engine.py",
        '''        limit = self.reinject_limit if limit is None else limit''',
        '''        limit = 25  # REVERTED: ignore config''',
        ["tests/test_config.py"],
    ),
    (
        "R16 core parity: diverge from core on private ranges",
        "ledger/url_safety.py",
        '''    if is_restricted_ip(ip) and not allow_private:
        return False, "private, loopback, link-local or reserved address"''',
        '''    if is_restricted_ip(ip) and not allow_private:
        return True, "ok"  # REVERTED: diverge from core''',
        ["tests/test_core_parity.py"],
    ),
    (
        "R17 git: drop the config overrides (repository can run core.fsmonitor)",
        "ledger/verifiers.py",
        '''_GIT_CONFIG_OVERRIDES = ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null")''',
        '''_GIT_CONFIG_OVERRIDES = ()  # REVERTED: no fsmonitor/hooks override''',
        ["tests/test_verifiers.py"],
    ),
    (
        "R18 git: run the child with the inherited host environment",
        "ledger/verifiers.py",
        '''            timeout=timeout,
            env=_minimal_env(),
        )''',
        '''            timeout=timeout,
        )  # REVERTED: host environment inherited''',
        ["tests/test_verifiers.py"],
    ),
]


def run(files):
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *files],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    tail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "(no output)"
    return proc.returncode, tail


def main():
    failures = 0
    print(f"{'revert':<58} {'result':<30} verdict")
    print("-" * 105)
    for name, rel, old, new, files in REVERTS:
        path = ROOT / rel
        original = path.read_text()
        if old not in original:
            print(f"{name:<58} {'PATTERN NOT FOUND':<30} n/a")
            failures += 1
            continue
        try:
            path.write_text(original.replace(old, new, 1))
            code, tail = run(files)
        finally:
            path.write_text(original)
        if code == 0:
            failures += 1
            verdict = "VACUOUS!"
        else:
            verdict = "ok (caught)"
        print(f"{name:<58} {tail:<30} {verdict}")
    print("-" * 105)
    if failures:
        print(f"{failures} revert(s) were NOT caught — those tests prove nothing.")
        return 1
    print("Every revert was caught: the security tests are non-vacuous.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
