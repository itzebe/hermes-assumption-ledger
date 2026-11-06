# Assumption Ledger

A persistent operational-assumption ledger for Hermes Agent.

An agent silently builds on things it has not checked: a file it believes exists, a branch it
believes it is on, a deployment it believes is live, a table it believes has rows. When one of
those beliefs is wrong, the failure surfaces much later and somewhere else. Assumption Ledger
captures those beliefs as first-class records, keeps their provenance, verifies them with
deterministic checks, and stops only the work that actually depends on a belief that has been
contradicted.

Everything is deterministic. No LLM call is made to extract or verify an assumption, so the
plugin needs no provider credentials and cannot be argued into a verdict.

- [What it does](#what-it-does)
- [The one rule that matters](#the-one-rule-that-matters)
- [Lifecycle](#lifecycle)
- [Install](#install)
- [Surfaces](#surfaces)
- [Example](#example)
- [Documentation](#documentation)
- [Configuration](#configuration)
- [Compatibility](#compatibility)
- [Known limitations](#known-limitations)
- [Privacy](#privacy)
- [Security](#security)
- [Tests](#tests)
- [License](#license)

## What it does

1. **Capture.** Assumptions come from explicit markers (`ASSUMPTION:`, `ASSUME:`,
   `PRECONDITION:`) in assistant messages and tool results, and from a small set of anchored
   natural-language patterns ("the file X exists", "on branch B", "HEAD is at commit C",
   "https://… returns 200"). Each record keeps the sentence it came from, so extraction is
   auditable rather than trusted. Tool results are scanned for explicit markers only — scanning raw
   tool output for patterns would manufacture assumptions the agent never made.
2. **Verify.** Each assumption carries a deterministic check. Twelve check kinds are supported:
   `file_exists`, `file_contains`, `file_hash`, `git_branch`, `git_commit`, `git_clean`,
   `env_set`, `command_succeeds`, `http_status`, `http_json_field`, `db_table_exists`,
   `db_row_count`. Every check returns exactly one of `pass`, `fail` or `error`.
3. **Track lifecycle.** Records move between `VALID`, `INVALIDATED`, `UNVERIFIED` and `STALE`.
4. **Propagate.** A decision that depends on an invalidated assumption is marked `blocked`. A
   pre-LLM gate stops the turn only when such a dependency exists, and a verify-gate holds
   edited-code turns the same way. Unrelated work is never blocked.
5. **Survive.** Records live in SQLite under the plugin data directory, so they outlive a
   compaction, a restart, and `hermes plugins update`.

## The one rule that matters

> Uncertainty is never an invalidation.

A provider timeout, an HTTP 429 or 5xx, a DNS failure, a rate limit or an unknown check kind
produce `UNVERIFIED` (or `STALE` if we previously held fresh evidence). They never produce
`INVALIDATED`. Only a definitive contradiction does. This is the difference between a ledger you
can trust and a false-alarm generator.

The rule cuts both ways, and the second direction is the one that actually protects you. A
contradiction already recorded is **not** softened by a failed re-check:

```
1. the config file is missing          -> fail  -> INVALIDATED
2. the deployment is blocked           -> blocked
3. the provider goes down, re-check    -> error
4. naive logic: error + fresh evidence -> STALE
5. STALE is not INVALIDATED            -> deployment unblocked   <-- the bug
```

Assumption Ledger stops at step 4. An outage is not evidence that a missing file reappeared. The
behaviour is implemented in a single function, `status_after_check`, and the sequence above is a
regression test.

## Lifecycle

| Status | Meaning | Blocks work? |
| --- | --- | --- |
| `VALID` | Evidence supports the assumption and is inside the freshness window. | No |
| `INVALIDATED` | Evidence definitively contradicts it. Dependent decisions are blocked. | **Yes** |
| `UNVERIFIED` | No usable evidence yet. The safe default. | No |
| `STALE` | Was valid; evidence aged out or could not be refreshed. Needs re-checking. | No |

A pass is never assumed to hold forever: `freshness_seconds` (default 86400) bounds how long
`VALID` lasts before degrading to `STALE`.

An `INVALIDATED` record is the one status uncertainty cannot move. A DNS failure, an HTTP 500, a
timeout or an unknown check kind leaves a contradiction standing, because "I could not re-check"
is not the same as "it is no longer true". Only evidence that refutes the contradiction clears it
and unblocks the decisions depending on it.

Two things surprise people. `UNVERIFIED` never blocks — a brand-new assumption is `UNVERIFIED`, and
if that blocked work, recording anything would halt the agent. And a gate requires a **dependent
decision**: an invalidated assumption nobody relies on blocks nothing.

`git_clean` reports untracked files as observed evidence but does not treat them as a dirty tree
by default, so a build artifact, a log or the ledger's own database cannot invalidate the
assumption. Pass `include_untracked: true` when untracked files genuinely matter.

## Install

Requires Hermes Agent `>=0.23.0` and Python `>=3.11`. **No third-party runtime dependencies.**

```bash
hermes plugins install itzebe/hermes-assumption-ledger --enable
```

Then confirm it loaded. Expect **4 tools and 4 hooks**:

```bash
hermes plugins doctor .
```

```
Plugin Doctor: .
  manifest: assumption-ledger 0.1.1 (standalone)
  OK: runtime discovery, manifest parsing, import, and registration passed
  registrations: 4 tool(s), 4 hook(s)
```

Records are written to `<hermes home>/plugin-data/assumption-ledger/ledger.db`.

> **Not yet in the official catalog.** The catalog entry is an open pull request
> ([NousResearch/hermes-agent#150212](https://github.com/NousResearch/hermes-agent/pull/150212)),
> not a merged listing. Use the `owner/repo` identifier shown above. The bare-name install
> (`hermes plugins install assumption-ledger`) fails until that PR merges, because a bare name is
> resolved only through the catalog.

Full instructions, including pinning a revision and removing the plugin, are in
[docs/INSTALLATION.md](docs/INSTALLATION.md).

## Surfaces

**Tools** — `assumption_record`, `assumption_check`, `assumption_status`, `assumption_decide`.

**Hooks** — `pre_llm_call` (re-inject live assumptions; block on a contradicted dependency),
`post_llm_call` (capture), `post_tool_call` (capture explicit markers), `pre_verify` (hold a
turn while a contradicted assumption is load-bearing).

**Slash command** — `/assumptions [status|list|check [id]|show <id>]`.

Every argument, return shape and error case is in [docs/API_REFERENCE.md](docs/API_REFERENCE.md).

## Example

```
ASSUMPTION: the file config/prod.json exists
ASSUMPTION: HEAD is at commit 486f4a6a9918a4054373e33a200260ffd501645e
```

The agent then records a decision:

```json
{"statement": "deploy the release", "depends_on": ["asm_...", "asm_..."]}
```

If `config/prod.json` turns out not to exist, that assumption becomes `INVALIDATED`, the deploy
decision is marked `blocked` with a reason naming it, and the next turn stops with an
explanation:

```
Assumption Ledger stopped this turn: a decision depends on an assumption that evidence has contradicted.
- INVALIDATED: the file config/prod.json exists (reason: path does not exist)
  blocks decision: deploy the release
Re-check the assumption or change the plan before continuing.
```

If instead the check merely timed out, the assumption stays `UNVERIFIED` and the turn proceeds.

[docs/USER_GUIDE.md](docs/USER_GUIDE.md) walks the full cycle with real captured output, including
what happens when the provider goes down mid-incident.

## Documentation

| Document | Read it for |
| --- | --- |
| [docs/VERIFICATION_AND_SAFETY.md](docs/VERIFICATION_AND_SAFETY.md) | **Start here to evaluate it.** Verdicts, the invariant, failure handling, what it does not guarantee. |
| [docs/INSTALLATION.md](docs/INSTALLATION.md) | Install, verify, update, remove. |
| [docs/USER_GUIDE.md](docs/USER_GUIDE.md) | Day-to-day use, with a complete worked example. |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How it is built: components, data model, data flow, state machine, diagrams. |
| [docs/API_REFERENCE.md](docs/API_REFERENCE.md) | Tools, hooks, slash command, and the Python API. |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | Check specifications, environment, storage paths. |
| [docs/SECURITY.md](docs/SECURITY.md) | Threat model, trust boundaries, data handling, limitations. |
| [docs/TESTING.md](docs/TESTING.md) | The suite, the frozen oracles, and the coverage gaps. |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | Diagnostic procedures for common symptoms. |
| [CHANGELOG.md](CHANGELOG.md) | Release history and known issues. |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Development setup and conventions. |

## Configuration

There is nothing to configure for normal use. `plugin.yaml` declares three options:

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `freshness_seconds` | int | 86400 | How long a pass stays `VALID` before it goes `STALE`. |
| `auto_check` | bool | true | Re-verify aged-out assumptions on the next check run. |
| `max_reinject` | int | 25 | Maximum live assumptions re-injected per turn. |

**These values are not currently read at runtime.** No code path reads them; the effective
behaviour uses compiled-in constants that happen to equal the declared defaults. So the table above
accurately describes what the plugin does, but setting these keys will pass validation and have no
effect. This is a documented gap, not a silent one — see
[docs/CONFIGURATION.md](docs/CONFIGURATION.md#declared-configuration-options-are-not-currently-read-at-runtime)
for the evidence and for what to do instead.

## Compatibility

| Item | Value |
| --- | --- |
| Hermes Agent | `>=0.23.0` |
| Python | `>=3.11` |
| Runtime dependencies | none |
| Required credentials | none |
| Registered hooks | `pre_llm_call`, `post_llm_call`, `post_tool_call`, `pre_verify` |
| Registered tools | `assumption_record`, `assumption_check`, `assumption_status`, `assumption_decide` |
| Slash command | `/assumptions` |
| Storage | SQLite (WAL) under `<hermes home>/plugin-data/assumption-ledger/` |

The plugin passes the upstream validator with 15/15 checks and zero warnings, including the
capability probe that imports the plugin in isolation, runs `register()`, and confirms the manifest
matches what was registered.

## Known limitations

Documented in full across [docs/](docs/) and
[CHANGELOG.md](CHANGELOG.md#known-issues). The ones most likely to matter:

- **Declared configuration options are not read at runtime.** See above.
- **`CheckContext.cwd` is unused.** Git and command checks resolve their directory from the check
  spec's `cwd` against `CheckContext.root`. The context's own `cwd` field is accepted, stored, and
  never read — a nonexistent `cwd` produces identical results.
- **Version string inconsistency.** `__init__.py` declares `0.1.0`; `plugin.yaml`,
  `pyproject.toml` and `ledger/__init__.py` declare `0.1.1`. The effective version is `0.1.1`.
- **No tags or releases.** `v0.1.1` exists only in the manifest; the catalog pins a commit SHA.
- **No CI.** There is no `.github/workflows/`; the suite is run locally.
- **No unstated-assumption detection.** If the agent never states the belief, the plugin cannot
  capture it.
- **`unsupported` checks never resolve.** A marker with no checkable form is stored and stays
  `UNVERIFIED` forever, so the stated belief is not lost.
- **The gate raises cost; it does not enforce.** An agent that never calls `assumption_decide` is
  never blocked.
- **`db_row_count`'s `where` clause is interpolated into SQL** and must be treated as trusted
  input. The `table` name is validated; the `where` clause is not.
- **`command_succeeds` executes whatever `argv` you give it.** It is documented as a read-only
  probe; nothing enforces that.

## Privacy

Everything is local. The plugin makes no network request of its own; the only outbound traffic
is the HTTP checks you explicitly record (`http_status`, `http_json_field`) against URLs you
name. Nothing is sent to the plugin author. Records are plain SQLite under
`<hermes home>/plugin-data/assumption-ledger/` and are yours to inspect or delete.

Environment-variable values are never recorded — `env_set` stores the name only, and a mismatch
records the fixed string "environment variable differs from expected". File contents are not
recorded; `file_contains` stores the pattern, not the matching text. Provenance does keep up to 500
characters of the sentence an assumption came from, and the plugin does not scan that for secrets.
Details in [docs/SECURITY.md](docs/SECURITY.md#sensitive-information-handling).

## Security

- **No network access of its own.** Only the HTTP checks you record.
- **No credentials.** No token, key or API key is read anywhere.
- **No LLM calls.** No prompt-injection surface in verification.
- **Local storage only.** One SQLite file. No telemetry, no analytics, no phone-home.

**This plugin has not been security-audited.** It passes the upstream automated security scan,
which is a linter result rather than an audit. There has been no third-party review and no
penetration test. Read [docs/SECURITY.md](docs/SECURITY.md) before relying on it for anything that
matters. Security reports go through the private advisory process described there, not public
issues.

## Tests

```
python -m pytest
```

**73 tests** across five files, with no network access, no skips and no xfails. The suite is
deterministic across runs.

| File | Tests | Focus |
| --- | --- | --- |
| `tests/test_frozen_oracles.py` | 17 | The adversarial expectation table. The contract. |
| `tests/test_verifiers.py` | 20 | Each of the 12 check kinds, plus failure modes. |
| `tests/test_engine.py` | 14 | Capture, verification, propagation, freshness, reporting. |
| `tests/test_plugin_integration.py` | 10 | Hooks, tools, slash command, manifest consistency. |
| `tests/test_extractor.py` | 12 | Extraction precision — including what must *not* be extracted. |

The oracle table covers the adversarial cases: correct evidence, contradictory evidence, missing
evidence, timeout, rate limit, server error, staleness, propagation, unrelated decisions, restart
recovery, compaction re-injection, conflicting evidence, malformed extraction, and duplicate
detection. Its expected values are frozen: a failing oracle means the change is wrong until proven
otherwise, and the docstring says so explicitly.

The uncertainty rule is protected by tests, and this was verified rather than assumed. Two
different ways of breaking the rule, with measured results:

| Break the rule by | Result |
| --- | --- |
| Removing the `INVALIDATED` guard, so a failed re-check degrades a contradiction to `STALE` | **1 failed, 72 passed** — `test_transient_error_does_not_soften_a_contradiction` |
| Mapping `error` to `INVALIDATED` (the naive rule) | **5 failed, 68 passed** — the four `UNVERIFIED` oracles plus `test_uncertainty_never_invalidates` |

The first row is the more important one. It is the *only* test that catches a contradiction being
softened by an outage, which is why the guard in `status_after_check` needs the suite to be run
rather than the change reasoned about. [docs/TESTING.md](docs/TESTING.md) records both results,
explains why `test_uncertainty_never_invalidates` does *not* catch the first case, and lists the
suite's coverage gaps honestly.

## License

MIT. See [LICENSE](LICENSE).
