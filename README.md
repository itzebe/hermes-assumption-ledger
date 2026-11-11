# Assumption Ledger

A persistent operational-assumption ledger for Hermes Agent: it captures the assumptions an agent
states in its own messages, keeps the sentence each came from as provenance, verifies them with
deterministic checks, and warns when work recorded in a session depends on an assumption that
evidence has contradicted.

**Who needs it.** Anyone running a long-lived Hermes agent that acts on the world — deploying,
editing, migrating, calling APIs. The agent silently builds on things it has not checked: a file it
believes exists, a branch it believes it is on, a deployment it believes is live, a table it
believes has rows. When one of those beliefs is wrong, the failure surfaces much later and
somewhere else.

**What it solves.** It turns those silent beliefs into first-class, inspectable records, each with a
deterministic pass/fail check, so a contradiction is caught where the belief was stated rather than
where it finally broke.

**How it works.** A `post_llm_call` hook captures explicit markers (`ASSUMPTION:`, `ASSUME:`,
`PRECONDITION:`) and a small set of anchored phrases from the assistant's **own message** — never
from tool output — and keeps the sentence each came from. A deterministic checker — one of twelve
kinds — returns exactly `pass`, `fail` or `error`. A `pre_llm_call` hook re-injects live assumptions
and warns when a session's recorded decision depends on a contradicted one. Everything is stored in
local SQLite under the plugin data directory.

**What makes it different.** Zero LLM calls in extraction or verification (no provider credentials,
and no prompt-injection surface in verification); uncertainty never becomes invalidation (a
timeout, 429 or 5xx yields `UNVERIFIED`, never `INVALIDATED`); provenance is retained;
contradictions survive transient verification failures; decisions are dependency-aware and
session-scoped; and recovery is always available through `resolve` or `drop`. It is an assumption
*ledger*, not a generic memory store.

**Install.** Requires Hermes Agent `>=0.23.0` and Python `>=3.11`; no third-party runtime
dependencies.

```bash
hermes plugins install assumption-ledger --enable        # from the official Hermes catalog
```

See [Install](#install) for the repository-pinned alternative and its review caveat.

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
   `PRECONDITION:`) in the assistant's own messages, and from a small set of anchored
   natural-language patterns ("the file X exists", "on branch B", "HEAD is at commit C",
   "https://… returns 200"). Each record keeps the sentence it came from, so extraction is
   auditable rather than trusted. **Tool output is never captured** — see "What is not captured"
   below.
2. **Verify.** Each assumption carries a deterministic check. Twelve check kinds are supported:
   `file_exists`, `file_contains`, `file_hash`, `git_branch`, `git_commit`, `git_clean`,
   `env_set`, `command_succeeds`, `http_status`, `http_json_field`, `db_table_exists`,
   `db_row_count`. Every check returns exactly one of `pass`, `fail` or `error`.
3. **Track lifecycle.** Records move between `VALID`, `INVALIDATED`, `UNVERIFIED`, `STALE` and
   `DROPPED`.
4. **Warn, then hold.** A decision that depends on an invalidated assumption is marked `blocked`,
   scoped to the session that recorded it. `pre_llm_call` injects a warning but never vetoes the
   turn — the agent needs that turn to investigate and clear the problem. The single hard gate is
   `pre_verify`, which holds an edited-code turn only when a decision *in that session* depends on a
   contradiction. Unrelated sessions are never affected, and the affected one always has a way out
   (`/assumptions resolve` or `/assumptions drop`).
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
| `DROPPED` | A user retired it without verification. Excluded from the live set. | No |

A pass is never assumed to hold forever: `freshness_seconds` (default 86400) bounds how long
`VALID` lasts before degrading to `STALE`.

An `INVALIDATED` record is the one status uncertainty cannot move. A DNS failure, an HTTP 500, a
timeout or an unknown check kind leaves a contradiction standing, because "I could not re-check"
is not the same as "it is no longer true". Only evidence that refutes the contradiction clears it
and unblocks the decisions depending on it.

`DROPPED` is deliberately not `VALID`. Retiring an assumption removes it from the live set and
unblocks dependent decisions, but it writes no pass verdict — the ledger records that a *user*
retired it, not that anything was verified.

Two things surprise people. `UNVERIFIED` never blocks — a brand-new assumption is `UNVERIFIED`, and
if that blocked work, recording anything would halt the agent. And a gate requires a **dependent
decision in the same session**: an invalidated assumption nobody relies on blocks nothing, and one
relied on in session A does not touch session B.

`git_clean` reports untracked files as observed evidence but does not treat them as a dirty tree
by default, so a build artifact, a log or the ledger's own database cannot invalidate the
assumption. Pass `include_untracked: true` when untracked files genuinely matter.

The git checks do not let a repository choose a program to run through `core.fsmonitor` or
`core.hooksPath`. Every `git` invocation is started with `-c core.fsmonitor=false -c
core.hooksPath=/dev/null` (a command-line `-c` beats the repository's own configuration) and the
minimal environment, and `git status` adds `--no-optional-locks`, so the host environment is not
visible to git or to anything it spawns. One vector remains by design: a `clean`/`smudge` filter
driver set in that repository's own `.git/config` still applies to `git status`, so point git checks
only at repositories whose local git config you trust. See
[docs/SECURITY.md](docs/SECURITY.md#git-checks-and-repository-controlled-execution).

## Install

Requires Hermes Agent `>=0.23.0` and Python `>=3.11`. **No third-party runtime dependencies.**

Assumption Ledger is in the **official Hermes plugin catalog** (merged
[#150212](https://github.com/NousResearch/hermes-agent/pull/150212)). Install it by its catalog
name — this resolves the reviewed entry and installs the pinned commit:

```bash
hermes plugins install assumption-ledger --enable
```

The `owner/repo` form also works, but it clones the repository's **default branch** rather than the
reviewed pin, and Hermes treats it as an **unreviewed custom source** — its install-time scan may
refuse it:

```bash
hermes plugins install itzebe/hermes-assumption-ledger --enable
```

Then confirm it loaded. Expect **6 tools and 4 hooks**:

```bash
hermes plugins doctor .
```

```
Plugin Doctor: .
  manifest: assumption-ledger 0.2.2 (standalone)
  OK: runtime discovery, manifest parsing, import, and registration passed
  registrations: 6 tool(s), 4 hook(s)
```

Records are written to `<hermes home>/plugin-data/assumption-ledger/ledger.db`.

> **Catalog pin.** The catalog entry pins
> `07bfaaaeb58fed1fa2d717f2c201e7bb17a61965` (v0.2.2). A catalog install is
> catalog-cached, so a newly published version can take a short while to appear. The repository's
> default branch (`main`) is still `v0.2.0`, so the two install methods are **not** equivalent —
> the catalog pin carries the security fixes that `main` does not yet contain.

Full instructions, including pinning a revision and removing the plugin, are in
[docs/INSTALLATION.md](docs/INSTALLATION.md).

## Surfaces

**Tools** — `assumption_record`, `assumption_check`, `assumption_status`, `assumption_decide`,
`assumption_resolve`, `assumption_drop`.

**Hooks** — `pre_llm_call` (re-inject live assumptions; warn, never veto, when a contradicted
assumption is load-bearing), `post_llm_call` (capture from the assistant's message),
`post_tool_call` (a deliberate no-op: tool output is never captured), `pre_verify` (the one hard
gate: hold an edited-code turn whose session recorded a decision on a contradicted assumption).

**Slash command** — `/assumptions [status|list|check [id]|show <id>|resolve <id>|drop <id>|config]`.

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
decision is marked `blocked` with a reason naming it, and the next turn carries a warning with the
commands that clear it:

```
Assumption Ledger warning: work recorded in this session depends on an assumption that evidence has contradicted.
- INVALIDATED: the file config/prod.json exists (id asm_7f3c1a)
  relied on by: deploy the release
This is a warning, not a stop. To clear it: /assumptions show <id>, /assumptions resolve <id> (re-check), or /assumptions drop <id> (retire).
```

The turn is not stopped, because the agent needs it to fix the problem. If the turn reaches the
edited-code verify gate with the block still standing, `pre_verify` holds it there, with the same
instructions. Creating `config/prod.json` and running `/assumptions resolve asm_7f3c1a` clears the
contradiction and unblocks the deploy decision. Retiring it instead with
`/assumptions drop asm_7f3c1a` also unblocks the decision, but records that nothing was verified.

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
| [llms.txt](llms.txt) | A concise, machine-readable entry point for AI assistants and research tools. |
| [docs/AI_DISCOVERY.md](docs/AI_DISCOVERY.md) | The repository's public discovery surfaces and how to verify them. |

## Configuration

There is nothing to configure for normal use. `plugin.yaml` declares five options, and all five
are read at runtime through the sanctioned `PluginContext.get_config` seam:

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `freshness_seconds` | int | 86400 | How long a pass stays `VALID` before it goes `STALE`. Bounded to 1..31536000. |
| `auto_check` | bool | false | Verify an assumption as soon as it is recorded, instead of leaving it `UNVERIFIED`. Off by default: it makes capture perform I/O. |
| `max_reinject` | int | 25 | Maximum live assumptions re-injected per turn. Bounded to 1..200. |
| `command_allowlist` | list | `[]` | Executable names `command_succeeds` may run, e.g. `[git, node]`. Empty disables command checks entirely. The allowlist restricts *which* program may run, not the safety of the arguments the model passes it — read the security note below before enabling it. |
| `allow_private_urls` | bool | false | Permit HTTP checks to reach loopback, private, link-local and CGNAT hosts. Cloud-metadata endpoints stay blocked regardless. |

A malformed value falls back to the default rather than disabling the plugin, and a config-backend
failure uses the safe defaults. `/assumptions config` prints the effective values.
`tests/test_config.py` asserts that each setting has an observable effect, so a setting that stops
being wired fails the suite instead of silently passing validation.

## Compatibility

| Item | Value |
| --- | --- |
| Hermes Agent | `>=0.23.0` |
| Python | `>=3.11` |
| Runtime dependencies | none |
| Required credentials | none |
| Registered hooks | `pre_llm_call`, `post_llm_call`, `post_tool_call`, `pre_verify` |
| Registered tools | `assumption_record`, `assumption_check`, `assumption_status`, `assumption_decide`, `assumption_resolve`, `assumption_drop` |
| Slash command | `/assumptions` |
| Storage | SQLite (WAL) under `<hermes home>/plugin-data/assumption-ledger/` |

The plugin passes the upstream validator with 15/15 checks and zero warnings, including the
capability probe that imports the plugin in isolation, runs `register()`, and confirms the manifest
matches what was registered. `hermes plugins doctor` reports 6 tools and 4 hooks.

## Known limitations

Documented in full across [docs/](docs/) and
[CHANGELOG.md](CHANGELOG.md#known-issues). The ones most likely to matter:

- **`CheckContext.cwd` is unused.** Git and command checks resolve their directory from the check
  spec's `cwd` against `CheckContext.root`. The context's own `cwd` field is accepted, stored, and
  never read — a nonexistent `cwd` produces identical results.
- **No tags or releases.** `v0.2.2` exists only as a version string in the manifest and modules; the
  catalog pins a commit SHA. The default branch (`main`) still reports `0.2.0` until the release
  branch is merged — the catalog pin, not `main`, is the reviewed code.
- **No CI.** There is no `.github/workflows/`; the suite is run locally.
- **No unstated-assumption detection.** If the agent never states the belief, the plugin cannot
  capture it.
- **`unsupported` checks never resolve.** A marker with no checkable form is stored and stays
  `UNVERIFIED` forever, so the stated belief is not lost.
- **The gate raises cost; it does not enforce.** An agent that never calls `assumption_decide` is
  never held at the verify gate.
- **The gate is per-session by design.** A block recorded in one session does not stop another.
  That is the correct default — cross-session enforcement would let one stale session halt all
  work — but it does mean a contradiction is not globally enforced.
- **`command_succeeds` is off until you enable it.** Execution requires a non-empty
  `command_allowlist`; the child gets a minimal environment; a check spec cannot widen the list.
  `argv[0]` must be a **bare executable name** — a path such as `/tmp/evil/git` or `./scripts/git`
  is refused, and the name is resolved to an absolute path on the child's own `PATH` before it is
  run.
- **The allowlist restricts the executable, not the arguments.** The model chooses the whole
  `argv`, so an allowed program can still be asked to do something dangerous. Allowing `node`
  permits `node -e '<arbitrary JavaScript>'`, which runs code with the child's environment and
  filesystem access. Allowing `git` permits a command that triggers a configured **git alias** or
  an external `core.editor`/`core.pager` helper — code you did not intend to run. Allowing a shell
  or an interpreter (`sh`, `bash`, `python`, `perl`) is equivalent to allowing arbitrary execution.
  The allowlist is a **name filter, not a sandbox**: treat every entry as "I trust this program to
  be driven by a model", keep the list as short as the checks genuinely need, and prefer programs
  whose only useful behaviour is to inspect state and exit.
- **HTTP checks follow redirects only after re-validating each hop.** A redirect to a blocked
  destination is refused, not followed.

## What is not captured

**Tool output is never stored as an assumption.** A web page, a file or a terminal listing can
contain the literal text `Assumption: ...`. Persisting that would write a durable record that is
re-injected into every later turn, which hands outside content a permanent place in the model's
context. `post_tool_call` is a deliberate no-op and `extract_from_tool_result` returns nothing.

Assumptions about a tool result belong in the assistant's own message, or in an explicit
`assumption_record` call. `tests/test_injection.py` covers this with planted markers in web pages,
file listings and terminal output.

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

**307 tests** across eleven files, with no network access and no xfails. The suite is
deterministic across runs. One test — the live-core comparison in `test_core_parity.py` — is
skipped on a bare checkout and runs only inside a Hermes checkout.

| File | Tests | Focus |
| --- | --- | --- |
| `tests/test_frozen_oracles.py` | 17 | The adversarial expectation table. The contract. |
| `tests/test_verifiers.py` | 34 | Each of the 12 check kinds, plus failure modes. |
| `tests/test_engine.py` | 14 | Capture, verification, propagation, freshness, reporting. |
| `tests/test_plugin_integration.py` | 12 | Hooks, tools, slash command, manifest consistency. |
| `tests/test_extractor.py` | 12 | Extraction precision — including what must *not* be extracted. |
| `tests/test_recovery.py` | 26 | Session-scoped blocking, `resolve`, `drop`, and the error invariant. |
| `tests/test_ssrf.py` | 67 | The SSRF policy: schemes, ranges, metadata, redirects, DNS. |
| `tests/test_command_policy.py` | 41 | Allowlist parsing, path refusal, resolution, minimal environment. |
| `tests/test_config.py` | 33 | Every declared setting has an observable effect. |
| `tests/test_injection.py` | 13 | Tool output is never captured; planted markers create nothing. |
| `tests/test_core_parity.py` | 39 | SSRF verdicts match Hermes core's `is_safe_url`. |

The oracle table covers the adversarial cases: correct evidence, contradictory evidence, missing
evidence, timeout, rate limit, server error, staleness, propagation, unrelated decisions, restart
recovery, compaction re-injection, conflicting evidence, malformed extraction, and duplicate
detection. Its expected values are frozen: a failing oracle means the change is wrong until proven
otherwise, and the docstring says so explicitly.

The uncertainty rule is protected by tests, and this was verified rather than assumed. Two
different ways of breaking the rule, with measured results:

| Break the rule by | Result |
| --- | --- |
| Removing the `INVALIDATED` guard, so a failed re-check degrades a contradiction to `STALE` | **2 failed, 305 passed** — `test_transient_error_does_not_soften_a_contradiction`, `test_drop_is_not_resurrected_by_an_error_recheck` |
| Mapping `error` to `INVALIDATED` (the naive rule) | **14 failed, 293 passed** — the four `UNVERIFIED` oracles, `test_uncertainty_never_invalidates`, and the six `test_error_paths_never_invalidate` cases |

The first row is the more important one. `test_transient_error_does_not_soften_a_contradiction` is
the only test that catches a contradiction being softened by an outage, which is why the guard in
`status_after_check` needs the suite to be run rather than the change reasoned about.
[docs/TESTING.md](docs/TESTING.md) records both results, explains why
`test_uncertainty_never_invalidates` does *not* catch the first case, and lists the suite's
coverage gaps honestly.

## License

MIT. See [LICENSE](LICENSE).
