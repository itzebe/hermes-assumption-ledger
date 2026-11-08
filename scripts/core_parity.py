#!/usr/bin/env python3
"""Prove the plugin's SSRF policy agrees with Hermes core's ``tools.url_safety.is_safe_url``.

The maintainer review asked for HTTP checks to behave like core's ``is_safe_url``. This script
imports that function from a real Hermes checkout and compares verdicts across a fixed corpus.

Usage:
    python3 scripts/core_parity.py <path-to-hermes-checkout>

Exit code 0 when every case agrees, 1 otherwise. It never touches the network: core's
``is_safe_url`` resolves DNS, and every host in the corpus is a literal IP or ``localhost``, so
resolution is local.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ledger.url_safety import check_url  # noqa: E402

CORPUS = [
    "https://example.com/",
    "http://example.com/x",
    "http://127.0.0.1/",
    "http://127.1.2.3/",
    "http://[::1]/",
    "http://localhost/",
    "http://0.0.0.0/",
    "http://10.0.0.1/",
    "http://192.168.1.1/",
    "http://172.16.5.5/",
    "http://[fc00::1]/",
    "http://169.254.1.1/",
    "http://[fe80::1]/",
    "http://100.64.0.1/",
    "http://169.254.169.254/latest/meta-data/",
    "http://169.254.170.2/",
    "http://169.254.169.253/",
    "http://100.100.100.200/",
    "http://[fd00:ec2::254]/",
    "http://[::ffff:169.254.169.254]/",
    "http://metadata.google.internal/",
    "http://metadata.goog/",
    "file:///tmp/example.txt",
    "ftp://example.com/",
    "gopher://x/",
    "data:text/plain,x",
    "ws://example.com/",
    "https://user:pass@example.com/",
    "http://example.com\\@127.0.0.1/",
    "http://example.com:99999/",
    "http://example.com%2f@127.0.0.1/",
    "http://127.0.0.1\u3002evil.com/",
    "https://",
    "not-a-url",
    "",
]


def core_commit(checkout: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10, check=True,
        )
        return out.stdout.strip()
    except Exception:
        return "unknown"


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    checkout = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(checkout))
    try:
        from tools.url_safety import is_safe_url
    except Exception as exc:
        print(f"cannot import tools.url_safety from {checkout}: {exc}")
        return 2

    mismatches = []
    for url in CORPUS:
        try:
            core = is_safe_url(url)
        except Exception as exc:
            core = f"raised {type(exc).__name__}"
        ours, reason = check_url(url)
        mark = "ok " if core == ours else "BAD"
        if core != ours:
            mismatches.append((url, core, ours))
        print(f"{mark} core={str(core):>5} ours={str(ours):>5}  {url!r}  ({reason})")

    print()
    print(f"core commit: {core_commit(checkout)}")
    print(f"{len(CORPUS)} cases, {len(mismatches)} mismatches")
    if mismatches:
        print("\nMISMATCHES:")
        for url, core, ours in mismatches:
            print(f"  {url!r}: core={core} ours={ours}")
        return 1
    print("PASS: the plugin's policy matches core's is_safe_url on every case.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
