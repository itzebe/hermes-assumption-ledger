# Changelog

All notable changes to Assumption Ledger are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Version status

**There are no git tags and no GitHub releases for this project.** `v0.2.1` exists only as a
version string in `plugin.yaml`, `pyproject.toml`, `__init__.py` and `ledger/__init__.py`. The
catalog entry pins a commit SHA, which is the authoritative reference for the reviewed code.

That means the entries below describe the state of the code, not tagged artifacts. Use the commit
SHA when you need an immutable reference.

### What the pinned commit does and does not contain

The catalog entry currently pins `650533a22434c330e6a0fa8f2ee7bb3147b5c029`, which is the commit the
maintainer reviewed for `v0.2.0`. That commit does not contain the `argv[0]` path fix described under
`[0.2.1]` below. The catalog entry is re-pinned to the new commit as part of the same change set.

The consequence for a reviewer: **the pinned SHA in the catalog must match the SHA that contains
the fixes you are reviewing.** If it does not, the catalog is stale.

### A note on version strings

All four version strings (`plugin.yaml`, `pyproject.toml`, `__init__.py`, `ledger/__init__.py`) are
`0.2.1` and agree. `tests/test_config.py::test_versions_agree_everywhere` fails if they diverge
again.

## [0.2.1] — address the second maintainer security review

Responds to the second `CHANGES_REQUESTED` review on catalog PR #150212, which reviewed `650533a2`.
**This is a security fix; upgrade deliberately.**

### Security

- **`argv[0]` must be a bare executable name.** Previously `argv[0]` was reduced to its basename, so
  a model-supplied path such as `/tmp/evil/git` or `./scripts/git` satisfied the `git` allowlist and
  ran an attacker-controlled file. A path is now **refused** on either platform, the bare name must
  be on the allowlist, the name is resolved with
  `shutil.which(name, path=minimal_env()["PATH"])`, resolution failure fails closed, and the
  **resolved absolute path** is executed instead of the model-supplied string.
- **The allowlist is documented as a name filter, not a sandbox.** The README, `plugin.yaml`,
  `docs/CONFIGURATION.md` and `docs/SECURITY.md` now state that the arguments are model-chosen, so
  an allowed `node` still runs `node -e '<code>'` and an allowed `git` can still invoke an alias or
  an external `core.editor`.

### Fixed

- **Re-injected blockers are session-scoped.** `render_reinjection` passed no session id to
  `critical_blockers`, so a contradiction recorded in one session was announced in the re-injected
  context of every other session. The `pre_llm_call` hook now passes the session id through, so the
  blocker section is scoped exactly as the hard gate is.
- **Relative imports.** The plugin entry point used top-level `from ledger import ...`, which only
  worked because a test loader put the plugin directory on `sys.path`. Hermes does not; the entry
  point now uses `from .ledger import ...`, and the tests load the module the way the real loader
  does.
- **Committed `.openhands/memory/` notes removed** and the directory added to `.gitignore`.

## [0.2.0] — address the maintainer security review

Responds to the `CHANGES_REQUESTED` review on catalog PR #150212. The review's concern was that
several checks "reach further than the read-only description says". This release narrows what a
check can reach, removes a prompt-injection channel, and makes blocking session-scoped and always
escapable. **This is a behaviour change; upgrade deliberately.**

### Security

- **`command_succeeds` is disabled by default.** Execution now requires a non-empty
  `command_allowlist` in plugin configuration. A check spec cannot widen the allowlist. The
  executable's basename must be listed, the child gets a minimal environment (`PATH`, empty
  `HOME`/`TMPDIR`, `LC_ALL=C`, `LANG=C`), and a refusal returns `error` — never `fail`. New module
  `ledger/command_policy.py`.
- **HTTP checks are screened for SSRF.** New module `ledger/url_safety.py` refuses non-HTTP
  schemes, loopback, RFC1918, CGNAT, link-local, unique-local and reserved addresses, and blocks
  cloud-metadata endpoints (`169.254.169.254`, `100.100.100.200`, `metadata.google.internal`, and
  IPv4-mapped forms) **even when `allow_private_urls` is true**. Every DNS answer is checked, so a
  public name resolving to a private address is refused. DNS failure fails closed. The policy
  deliberately mirrors core's `tools.url_safety.is_safe_url`, and `scripts/core_parity.py` proves
  agreement with it (35/35 cases at core `ff6781e8e1`).
- **Redirects are re-validated per hop.** Redirects are followed manually, up to 5 hops, with the
  policy applied to each target. A redirect to a blocked destination is refused, not followed.
- **`db_row_count`'s `where` clause is validated.** It must match a simple-predicate grammar
  (`column OP literal` joined by `AND`/`OR`, plus `IS [NOT] NULL`). Subqueries, function calls,
  comments and semicolons are refused and return `error`.
- **Tool output is no longer a capture source.** `post_tool_call` is a no-op and
  `extract_from_tool_result` returns nothing. Previously a planted `Assumption: ...` marker in a
  web page, file or terminal listing became a durable record re-injected into every later turn.
- **`http_json_field` evidence is bounded and type-tagged.** The resolved value is summarised
  through `_summarize` — scalars pass through, a string is truncated to 120 characters, and a list
  or dict becomes `{"type": ..., "len"/"keys": ...}` — so an endpoint cannot plant an arbitrary
  unbounded string into evidence that is later re-injected. The pointer and the match result are
  recorded too.

### Changed

- **`pre_llm_call` warns; it no longer blocks.** It returns context with the recovery commands
  instead of `{"action": "block"}`. Blocking before inference stopped the turn the agent needed to
  investigate, in every session. The one hard gate is now `pre_verify`.
- **Blocking is session-scoped.** Decisions carry a `session_id`; a contradiction recorded in one
  session no longer holds another. Existing databases are migrated in place.
- **Every declared setting is now read at runtime** through `PluginContext.get_config`:
  `freshness_seconds`, `auto_check`, `max_reinject`, plus the new `command_allowlist` and
  `allow_private_urls`. Malformed values fall back to defaults; an out-of-range integer is clamped.
  `auto_check` now defaults to `false`.
- `pre_verify`'s message names the recovery commands, so a held turn is always escapable.

### Added

- **`DROPPED` status and `/assumptions drop <id>`** — retire an assumption without verification.
  It leaves the live set and unblocks its decisions, and records a `dropped` evidence row. It never
  writes a pass verdict.
- **`/assumptions resolve <id>` and the `assumption_resolve` tool** — re-check an assumption and
  report the outcome. It cannot force a status: a still-failing check keeps the block.
- **`assumption_drop` tool**, and `/assumptions config` to print effective settings.
- `ledger/store.py` migrates an existing `decisions` table to add `session_id`.
- Test suites `test_ssrf.py` (67), `test_recovery.py` (24), `test_injection.py` (13),
  `test_config.py` (33), `test_command_policy.py` (28), `test_core_parity.py` (39). Total
  **284 passing, 1 skipped** (the skip is the live-core comparison, which runs inside a Hermes
  checkout).
- `scripts/core_parity.py <hermes-checkout>` — proves the SSRF policy matches core's
  `tools.url_safety.is_safe_url`. Verified at core `ff6781e8e1`: 35 cases, 0 mismatches.
- `scripts/nonvacuous_check.py` — reverts each hardening change and asserts the suite catches it.
  All 16 reverts are caught.

### Fixed

- A redirect returned as a normal 3xx response (rather than raised as `HTTPError`) did not expose
  its `Location`, so it was never followed or re-validated. Both paths now report it.

### Documentation

- README, `docs/ARCHITECTURE.md`, `docs/API_REFERENCE.md`, `docs/SECURITY.md`,
  `docs/CONFIGURATION.md`, `docs/USER_GUIDE.md`, `docs/TROUBLESHOOTING.md`, `docs/TESTING.md` and
  `docs/VERIFICATION_AND_SAFETY.md` corrected to match the new behaviour. The previous docs
  described blocking, tool-output capture, unread configuration and the two unguarded check kinds;
  all four are now obsolete.

## [0.1.2] — complete documentation set

Documentation-only change on top of `0.1.1`. No production code changed.

### Added

- Complete project documentation under `docs/`:
  - `ARCHITECTURE.md` — components, data model, data flow, state machine, four Mermaid diagrams.
  - `VERIFICATION_AND_SAFETY.md` — verdict semantics, the lifecycle invariant, failure handling,
    and an explicit list of what the plugin does not guarantee.
  - `API_REFERENCE.md` — every tool, hook, slash command and Python API, with real captured output.
  - `CONFIGURATION.md` — check specifications, environment variables, storage paths.
  - `INSTALLATION.md` — install, verify, update, remove.
  - `USER_GUIDE.md` — day-to-day use with a complete worked example.
  - `SECURITY.md` — threat model, trust boundaries, data handling, known limitations.
  - `TESTING.md` — the suite, the frozen oracles, and the coverage gaps.
  - `TROUBLESHOOTING.md` — diagnostic procedures.
- `CONTRIBUTING.md` and this changelog.
- README expanded into a full project entry point with a documentation index.

### Fixed

- **Documentation corrected against the source.** Several claims in the previous README did not
  match the implementation:
  - The test count was given as 72. The actual count is **73**.
  - "Reverting the uncertainty-never-invalidates rule fails four of them" was imprecise. Measured:
    removing the `INVALIDATED` guard fails **one** test; mapping `error` to `INVALIDATED` fails
    **five**. Both numbers are now recorded.
  - The configuration table presented `freshness_seconds`, `auto_check` and `max_reinject` as
    working settings. They are declared in `plugin.yaml` and never read by any code path.
  - The install section implied the catalog was available. It is an open pull request.

No production code was changed by this work. It is a documentation-only change.

## [0.1.1] — pin `388cf029ccc5324c3f246423c8d461aa93d60090`

The state of the plugin as submitted to the Hermes plugin catalog. Untagged.

### Added

- **Engine** (`ledger/engine.py`) — the single decision point for lifecycle status, plus
  propagation, freshness sweeping and reporting.
- **Model** (`ledger/model.py`) — records, lifecycle constants, deterministic content-addressed
  IDs, and `status_after_check`, the one rule the engine obeys.
- **Extractor** (`ledger/extractor.py`) — conservative rule-based capture. Explicit markers
  (`ASSUMPTION:`, `ASSUME:`, `PRECONDITION:`, `assuming`) in messages and tool results, plus
  anchored natural-language patterns in messages only.
- **Verifiers** (`ledger/verifiers.py`) — 12 deterministic checks returning `pass` / `fail` /
  `error`, with `CheckContext` as the injection seam for tests.
- **Store** (`ledger/store.py`) — SQLite persistence in WAL mode, process-safe, with an injectable
  path so the engine is storage-agnostic.
- **Hermes adapter** (`__init__.py`) — 4 hooks, 4 tools, 1 slash command, and lazy engine
  construction so an import failure degrades to a no-op rather than a crash.
- 73 tests across five files, including a frozen oracle table for the adversarial scenarios.

### Design decisions

- **No LLM call in extraction or verification.** The plugin needs no provider credentials and has
  no prompt-injection surface in verification.
- **Uncertainty never invalidates.** `error` verdicts produce `UNVERIFIED` or `STALE`, never
  `INVALIDATED`.
- **A contradiction survives a failed re-check.** An existing `INVALIDATED` record is not degraded
  by a timeout, rate limit or outage. Only refuting evidence clears it.
- **Tool results are scanned for markers only.** Natural-language patterns are not applied to tool
  output, so evidence text cannot manufacture an assumption.
- **Only invalidated dependencies block.** `VALID`, `STALE` and `UNVERIFIED` do not block, and an
  invalidated assumption with no dependent decision blocks nothing.

## Known issues

State as of `0.2.0`. Issues fixed in this release are listed first so the review trail is intact.

### Resolved in 0.2.0

| ID | Issue | Resolution |
| --- | --- | --- |
| KI-1 | Declared configuration options were never read | All five are now read through `PluginContext.get_config`, with `test_config.py` asserting an observable effect for each. |
| KI-3 | Inconsistent version strings | All four strings are `0.2.0`; `test_versions_agree_everywhere` guards it. |
| KI-6 | `db_row_count` interpolated the `where` clause | The clause is validated against a simple-predicate grammar; a refusal returns `error`. |
| KI-7 | `command_succeeds` ran an arbitrary `argv` | Execution is off by default, gated by a configured `command_allowlist`, and runs with a minimal environment. |
| KI-10 | `http_json_field` stored the resolved value unbounded | Evidence now stores a bounded, type-tagged summary (`_summarize`); a string is truncated to 120 characters. A secret in a statement is still stored. |

### KI-2: `CheckContext.cwd` is accepted but never read

`CheckContext.__init__` stores `self.cwd`, and nothing reads it. Git and command checks resolve
their working directory from `spec.get("cwd", ".")` **against `CheckContext.root`**.

**Impact:** passing `CheckContext(cwd=...)` has no effect. Verified: a nonexistent `cwd` produces
identical results to a valid one. Under Hermes the adapter passes `root == cwd`, so the distinction
is invisible in the default path — but an embedder setting only `cwd` will be surprised.

**Workaround:** set `cwd` in the check spec:
`{"kind": "git_branch", "branch": "main", "cwd": "/path/to/repo"}`.

### KI-4: No git tags or releases

`v0.2.0` exists only in the manifest. There is no immutable tag to reference.

**Workaround:** use the pinned commit SHA from the catalog entry.

### KI-5: No continuous integration

There is no `.github/workflows/` directory. The suite is run locally.

**Impact:** nothing enforces that tests pass before a merge. `scripts/nonvacuous_check.py` runs the
revert-and-catch check, but it is manual. A suggested minimal workflow is in `docs/TESTING.md`.

### KI-8: Absolute paths in check specs bypass the workspace root

`CheckContext.resolve` returns an absolute path unchanged. `root` is a convenience for relative
paths, not a sandbox boundary.

**Impact:** a check can read any file the process can read.

**Documented in:** `docs/SECURITY.md`.

### KI-9: The database is unencrypted and has no integrity protection

Plain SQLite, no encryption, no password, no signatures, no hash chain.

**Impact:** anyone who can read the file can read every assumption and its evidence, and a local
actor can edit it undetected. Protect it with filesystem permissions. Records are trustworthy
against accidental loss, not against a determined local adversary.

**Documented in:** `docs/SECURITY.md`.

### KI-11: `unsupported` checks never resolve

A marker with no checkable form is stored with `{"kind": "unsupported"}` and stays `UNVERIFIED`
forever. This is intentional — the stated belief is preserved rather than discarded — but it means
such records are permanent noise in the status view.

### KI-12: No unstated-assumption detection

If the agent never states the belief and never writes a marker, the plugin cannot capture it.
Extraction is conservative by design, so it misses things.

### KI-13: The gate raises cost, it does not enforce

`pre_verify` holds a turn only when a **recorded decision in the same session** depends on an
invalidated assumption. An agent that never calls `assumption_decide` is never held. Hermes also
caps verify-gate nudges, so a determined agent can proceed.

**This is deliberate.** The alternative — blocking inference in every session — was the `0.1.1`
behaviour and was removed because it stopped the turn the agent needed to investigate, everywhere
at once.

### KI-14: The allowlist constrains the executable, not the arguments

A bare allowlisted name is resolved through `PATH`, so an attacker who can write an executable
named `git` earlier on the path could still be run — keep `PATH` under your control. More
importantly, the allowlist restricts *which* program runs, not what it does with the model-chosen
arguments: an allowed `node` still runs `node -e '<code>'`, and an allowed `git` can still invoke a
configured alias or an external `core.editor`.

**Impact:** the allowlist reduces the capability, it does not eliminate it. It is a name filter,
not a sandbox. Keep it short and keep `PATH` under your control.
