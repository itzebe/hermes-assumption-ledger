"""Command-execution policy for the ``command_succeeds`` verifier.

The verifier exists because "this command exits 0" is sometimes the only honest way to check an
operational assumption. But a model-supplied argv executed on the host is arbitrary code
execution outside Hermes' terminal approval and sandbox, so the default is **no execution at
all**: the verifier is disabled unless the user has named an allowlist in plugin config.

The policy, in full:

* Disabled unless ``command_allowlist`` is non-empty.
* Only an executable whose *basename* is on the allowlist may run. The basename is used because
  the model chooses the path, and ``/usr/bin/git`` and ``/tmp/evil/git`` would otherwise be
  indistinguishable. This is a name check, not a path check, and is documented as such.
* Arguments are passed as a literal argv with ``shell=False``. No shell, no interpolation.
* The child gets a minimal environment: ``PATH`` plus an empty ``HOME``/``TMPDIR``. The host
  environment is never inherited, so ``env_set``-style secrets are not visible to a child.
* A model may not widen the allowlist. Only plugin config supplies it, and config is read from
  the Hermes settings subtree, not from a check spec.
"""
from __future__ import annotations

import os
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Longest allowlist we will honour. A user who needs more than this is configuring something the
# plugin is not the right tool for.
MAX_ALLOWLIST_ENTRIES = 32

# Characters that cannot appear in an executable name. Deliberately excludes ``/`` and ``\`` so a
# path is rejected rather than silently basename'd.
_ALLOWED_NAME_RE = __import__("re").compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")


def normalize_allowlist(raw: Any) -> Tuple[str, ...]:
    """Parse a user-supplied allowlist into a tuple of bare executable names.

    Accepts a list/tuple of strings, or a comma/whitespace separated string. Entries that look
    like paths, are empty, contain separators or shell metacharacters, or exceed
    :data:`MAX_ALLOWLIST_ENTRIES` are dropped. Dropping is deliberate: a rejected entry can only
    ever *remove* permission, never grant it.
    """
    if raw is None:
        return ()
    if isinstance(raw, str):
        entries: Sequence[Any] = [p for p in raw.replace(",", " ").split() if p]
    elif isinstance(raw, (list, tuple, set, frozenset)):
        entries = list(raw)
    else:
        return ()

    out: List[str] = []
    for entry in entries:
        if not isinstance(entry, str):
            continue
        name = entry.strip()
        if not name or name in (".", ".."):
            continue
        # A path, on either platform, is not a bare name and is refused outright.
        if "/" in name or "\\" in name:
            continue
        if name != PurePosixPath(name).name or name != PureWindowsPath(name).name:
            continue
        if not _ALLOWED_NAME_RE.match(name):
            continue
        if name not in out:
            out.append(name)
        if len(out) >= MAX_ALLOWLIST_ENTRIES:
            break
    return tuple(out)


def executable_name(argv: Any) -> Optional[str]:
    """The basename of ``argv[0]``, or None when *argv* is not a usable argv."""
    if not isinstance(argv, (list, tuple)) or not argv:
        return None
    first = argv[0]
    if not isinstance(first, str) or not first.strip():
        return None
    name = first.strip()
    if "/" in name or "\\" in name:
        name = PurePosixPath(PureWindowsPath(name).name).name
    return name or None


def is_allowed(argv: Any, allowlist: Sequence[str]) -> Tuple[bool, str]:
    """Decide whether *argv* may be executed under *allowlist*.

    Returns ``(allowed, reason)``. *reason* is a short machine-readable tag, never the argv, so a
    caller can log a refusal without echoing model input.
    """
    if not allowlist:
        return False, "command checks are disabled (no command_allowlist configured)"
    if not isinstance(argv, (list, tuple)) or not argv:
        return False, "argv must be a non-empty list"
    if any(not isinstance(a, str) for a in argv):
        return False, "argv entries must be strings"
    if len(argv) > 256:
        return False, "argv too long"
    name = executable_name(argv)
    if name is None:
        return False, "no executable name"
    if name not in allowlist:
        return False, f"executable {name!r} is not on the command allowlist"
    return True, "ok"


def minimal_env(path: Optional[str] = None) -> Dict[str, str]:
    """The environment a permitted child receives.

    Deliberately tiny: ``PATH`` so the executable resolves, plus empty ``HOME`` and ``TMPDIR`` so
    a tool that writes a dotfile or temp file lands somewhere predictable rather than in the
    user's real home. No host variable is inherited.
    """
    return {
        "PATH": path or os.environ.get("PATH") or "/usr/local/bin:/usr/bin:/bin",
        "HOME": "",
        "TMPDIR": "",
        "LC_ALL": "C",
        "LANG": "C",
    }
