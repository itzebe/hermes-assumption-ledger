# Changelog

All notable changes to Assumption Ledger are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Version status

**There are no git tags and no GitHub releases for this project.** `v0.1.1` exists only as a
version string in `plugin.yaml`, `pyproject.toml` and `ledger/__init__.py`. The catalog entry pins
the commit `388cf029ccc5324c3f246423c8d461aa93d60090`, which is the authoritative reference for the
reviewed `v0.1.1` code.

That means the entries below describe the state of the code, not tagged artifacts. Use the commit
SHA when you need an immutable reference.

### A note on version strings

`__init__.py` declares `__version__ = "0.1.0"`, while `plugin.yaml`, `pyproject.toml` and
`ledger/__init__.py` all declare `0.1.1`. This is an inconsistency, not a hidden release.

The **effective** version is `0.1.1`: that is what Hermes reads from the manifest, what
`hermes plugins doctor` reports, and what `ledger.__version__` returns. The `0.1.0` string in the
adapter module is stale. If you need the version programmatically, read `plugin.yaml` or
`ledger.__version__` — not the adapter module.

## [Unreleased]

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

These are recorded as known issues rather than fixed silently, because fixing them changes
behaviour and this changelog accompanies a documentation change.

### KI-1: Declared configuration options are never read

`plugin.yaml` declares `freshness_seconds` (86400), `auto_check` (true) and `max_reinject` (25).
No code path reads any of them.

| Option | Actual behaviour |
| --- | --- |
| `freshness_seconds` | The engine uses `DEFAULT_FRESHNESS_SECONDS = 24 * 60 * 60`, equal to the declared default. |
| `max_reinject` | `on_pre_llm_call` calls `render_reinjection()` with no argument, so the `limit=25` default applies. |
| `auto_check` | No code path at all. `sweep_stale()` is unconditional. |

**Impact:** behaviour matches the declared defaults exactly, so the manifest is not misleading about
what the plugin does. The limitation is that the values cannot be tuned. Setting them passes
Hermes' schema validation and has no effect. `auto_check: false` is not implementable today.

**Workaround:** embed the engine directly —
`LedgerEngine(store, freshness_seconds=...)` — or call `render_reinjection(limit=...)` yourself.
The Hermes plugin surface does not expose either.

**Evidence:** `grep -rn "get_config\|ctx.config\|register_config" --include=*.py .` returns no
matches. Hermes' `_validate_plugin_config_schema` is validation-only: it warns, never blocks, and
does not inject defaults.

**Documented in:** `docs/CONFIGURATION.md`.

### KI-2: `CheckContext.cwd` is accepted but never read

`CheckContext.__init__` stores `self.cwd`, and nothing reads it. Git and command checks resolve
their working directory from `spec.get("cwd", ".")` **against `CheckContext.root`**.

**Impact:** passing `CheckContext(cwd=...)` has no effect. Verified: a nonexistent `cwd` produces
identical results to a valid one. Under Hermes the adapter passes `root == cwd`, so the distinction
is invisible in the default path — but an embedder setting only `cwd` will be surprised.

**Workaround:** set `cwd` in the check spec:
`{"kind": "git_branch", "branch": "main", "cwd": "/path/to/repo"}`.

**Documented in:** `docs/CONFIGURATION.md`, `docs/TROUBLESHOOTING.md`.

### KI-3: Inconsistent version string

`__init__.py` declares `0.1.0`; `plugin.yaml`, `pyproject.toml` and `ledger/__init__.py` declare
`0.1.1`. The effective version is `0.1.1`.

**Impact:** cosmetic, with one trap: a compatibility check that imports the version from the
adapter module reads the wrong value.

**Workaround:** read `plugin.yaml` or `ledger.__version__`.

### KI-4: No git tags or releases

`v0.1.1` exists only in the manifest. There is no immutable tag to reference.

**Workaround:** use the commit SHA `388cf029ccc5324c3f246423c8d461aa93d60090`, which the catalog
entry pins and which remains reachable.

### KI-5: No continuous integration

There is no `.github/workflows/` directory. The suite is run locally.

**Impact:** nothing enforces that tests pass before a merge. A suggested minimal workflow is in
`docs/TESTING.md`.

### KI-6: `db_row_count` interpolates the `where` clause into SQL

The `table` name is validated against `[A-Za-z_][A-Za-z0-9_]*`; the `where` clause is not validated
or parameterised. The connection is read-only, which limits the blast radius but does not make an
arbitrary predicate safe.

**Impact:** a `where` value must be treated as trusted input. Do not populate it from model output.

**Documented in:** `docs/SECURITY.md`.

### KI-7: `command_succeeds` executes an arbitrary `argv`

The check is documented as a read-only probe, and nothing enforces that. `ctx.runner(argv, ...)`
runs whatever is specified, with the plugin's privileges. No shell is involved, so shell
metacharacters are not interpreted.

**Impact:** an agent that can record a check spec can run a command. That is a capability of the
plugin, not a vulnerability in it, but it should be a conscious decision.

**Documented in:** `docs/SECURITY.md`.

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

### KI-10: No secret scanning before storage

Provenance keeps up to 500 characters of the source sentence, and `http_json_field` evidence keeps
the resolved value. Neither is scanned for secrets. Environment-variable values are deliberately
never recorded, and file contents are not recorded — but a secret in a sentence the agent wrote
will be stored.

**Documented in:** `docs/SECURITY.md`.

### KI-11: `unsupported` checks never resolve

A marker with no checkable form is stored with `{"kind": "unsupported"}` and stays `UNVERIFIED`
forever. This is intentional — the stated belief is preserved rather than discarded — but it means
such records are permanent noise in the status view.

### KI-12: No unstated-assumption detection

If the agent never states the belief and never writes a marker, the plugin cannot capture it.
Extraction is conservative by design, so it misses things.

### KI-13: The gate raises cost, it does not enforce

`pre_llm_call` blocks only when a **recorded decision** depends on an invalidated assumption. An
agent that never calls `assumption_decide` is never blocked. Hermes also caps verify-gate nudges,
so a determined agent can proceed past `pre_verify`.
