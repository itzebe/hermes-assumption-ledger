# Contributing

Thanks for considering a contribution. This document covers the development setup, the conventions
this codebase follows, and the constraints that matter when changing behaviour.

- [Development setup](#development-setup)
- [Project layout](#project-layout)
- [Running the tests](#running-the-tests)
- [Code conventions](#code-conventions)
- [The rules you must not break](#the-rules-you-must-not-break)
- [Adding a verifier](#adding-a-verifier)
- [Adding an extractor pattern](#adding-an-extractor-pattern)
- [Changing the lifecycle rule](#changing-the-lifecycle-rule)
- [Documentation](#documentation)
- [Submitting a change](#submitting-a-change)
- [Known issues you could fix](#known-issues-you-could-fix)

## Development setup

```bash
git clone https://github.com/itzebe/hermes-assumption-ledger
cd hermes-assumption-ledger
pip install -e ".[dev]"
```

That is the whole setup. There are **no runtime dependencies** — `dependencies = []` in
`pyproject.toml`. The plugin uses only the standard library, so there is nothing to install beyond
pytest, and nothing that can drift.

You do **not** need Hermes installed to develop or test the engine. `ledger/` has no Hermes import;
`__init__.py` is the only Hermes-aware file and imports `ledger` lazily.

To run the upstream validator as well, you need Hermes:

```bash
hermes plugins validate .
hermes plugins doctor .
```

## Project layout

```
.
├── __init__.py            # The Hermes adapter. The only Hermes-aware file.
├── plugin.yaml            # Manifest: name, version, hooks, tools, config schema.
├── pyproject.toml         # Packaging and pytest config.
├── ledger/
│   ├── __init__.py        # Public exports and __version__.
│   ├── engine.py          # Lifecycle rules, propagation, freshness, reporting.
│   ├── model.py           # Records, statuses, IDs, status_after_check.
│   ├── extractor.py       # Deterministic rule-based capture.
│   ├── verifiers.py       # The 12 checks and CheckContext.
│   └── store.py           # SQLite persistence.
├── tests/
│   ├── test_frozen_oracles.py     # The contract. 17 tests.
│   ├── test_verifiers.py          # The checks. 30 tests.
│   ├── test_engine.py             # Engine behaviour. 14 tests.
│   ├── test_plugin_integration.py # The adapter. 12 tests.
│   └── test_extractor.py          # Extraction precision. 12 tests.
├── docs/                  # User and developer documentation.
├── CHANGELOG.md
├── CONTRIBUTING.md
└── LICENSE
```

The dependency direction is one-way: `__init__.py` → `ledger/`. Never import Hermes or the adapter
from inside `ledger/`. That separation is what keeps the engine testable without Hermes, and it is
worth protecting.

## Running the tests

```bash
python -m pytest -q                    # 303 passed, ~2s
python -m pytest -x                    # stop at first failure
python -m pytest tests/test_frozen_oracles.py -v
```

There is no CI configuration in this repository (see
[KI-5](CHANGELOG.md#ki-5-no-continuous-integration)), so **run the suite locally before you push.**
Adding a workflow would itself be a welcome contribution; a minimal one is sketched in
[docs/TESTING.md](docs/TESTING.md#continuous-integration).

The suite must stay deterministic: no network access, no sleeps, no order dependence. Three
consecutive runs should produce identical results.

## Code conventions

These are the conventions the existing code follows. Match them.

**Standard library only.** No new runtime dependencies. If a change seems to need one, that is a
design conversation, not a dependency bump. The zero-dependency property is load-bearing: it is why
the plugin works with no credentials and why an update cannot break an environment.

**Determinism over cleverness.** No randomness, no wall-clock dependence in logic, no model calls.
Where time matters, take an injectable `now` (as `LedgerEngine` does). Where I/O matters, take an
injectable seam (as `CheckContext` does).

**Failures become uncertainty.** Any unexpected condition in a check path must become an `ERROR`
verdict, never a `pass` or a `fail`. `run_check` normalises this centrally — an unknown kind, a
non-dict spec, a raising verifier, a wrong return type and an unknown verdict all become `ERROR`.
If you add a verifier, return a proper `CheckResult`; do not raise, and do not invent a verdict.

**Hook callbacks fail open.** Every hook wraps its body in `try/except` and returns `None` on
failure. A plugin bug must degrade to "did nothing this turn", never break the agent loop.

**Tool handlers never raise.** Each returns a JSON string, and on error returns
`{"error": "..."}`.

**Comment the non-obvious, not the obvious.** Explain *why* where the reason is not visible from
the code — an invariant, a deliberate omission, a subtle ordering. Do not narrate what the next
line does, and do not describe the change you are making. The existing docstrings are the model to
follow: they state contracts and reasons.

**Type hints on public functions.** The codebase uses `from __future__ import annotations` and
annotates parameters and returns.

## The rules you must not break

These are the product's promises. A change that violates one is a bug, regardless of what it
improves elsewhere.

### 1. Uncertainty never invalidates

`error` must never produce `INVALIDATED`. A timeout, rate limit, DNS failure, 5xx, missing file or
unknown check kind is uncertainty.

### 2. A contradiction survives a failed re-check

An existing `INVALIDATED` record must not be degraded by an `error`. This is the guard in
`status_after_check`, and it is the reason the plugin exists. Removing it lets a provider outage
silently unblock work that depends on a known-broken assumption.

### 3. Only invalidated dependencies block

`VALID`, `STALE` and `UNVERIFIED` do not block. An invalidated assumption with no dependent
decision blocks nothing.

### 4. Tool results are scanned for markers only

Do not apply natural-language patterns to tool output. Evidence text would then manufacture
assumptions the agent never made.

### 5. No LLM call in extraction or verification

Not as a fallback, not behind a flag, not for "hard cases". The zero-credential and
no-prompt-injection properties depend on it.

### 6. The oracle table is frozen

If a change makes an oracle fail, the change is wrong until proven otherwise. Do not edit an
expected value to make a test pass. `tests/test_frozen_oracles.py` states this in its docstring, and
changing an oracle means changing the product's contract — a deliberate decision, to be recorded in
[CHANGELOG.md](CHANGELOG.md).

## Adding a verifier

1. Write the check function in `ledger/verifiers.py`, taking `(spec, ctx)` and returning a
   `CheckResult`. Use the `_ok` / `_bad` / `_unknown` helpers — they set the verdict consistently.
2. Return `_unknown(...)` for **every** unexpected condition. Never let an exception escape.
3. Register it in the `VERIFIERS` dict.
4. Update `SUPPORTED_KINDS` — a test asserts the registry is closed, so this is enforced.
5. Add tests to `tests/test_verifiers.py`: pass, fail, and at least one error path. Use an injected
   `runner` or `http_get` rather than touching the network.
6. Document the spec fields in `docs/CONFIGURATION.md`, and the kind in the README table and
   `docs/API_REFERENCE.md`.

If the check needs new I/O, add it to `CheckContext` as an injectable seam so it stays testable
without that I/O. That is what `http_get` and `runner` are for.

## Adding an extractor pattern

Extraction is where over-reach is easiest and most damaging. A pattern that is too loose creates
assumptions the agent never made, which is worse than missing one.

1. Add the pattern to `ledger/extractor.py`, in the anchored style the existing ones use.
2. Make it produce a check spec that is actually verifiable. If it cannot, do not add the pattern.
3. Add a **negative** test as well as a positive one. Prose that should extract nothing is at least
   as important as prose that should extract something.
4. Confirm the pattern is not applied to tool results. `extract_from_tool_result` must keep calling
   `extract_from_text` with `allow_patterns=False`.
5. Update the pattern table in [docs/USER_GUIDE.md](docs/USER_GUIDE.md#writing-assumptions-the-extractor-understands).

## Changing the lifecycle rule

`status_after_check` is the single decision point for every status change. Treat it as load-bearing
code.

1. Read [docs/VERIFICATION_AND_SAFETY.md](docs/VERIFICATION_AND_SAFETY.md#the-critical-invariant)
   first. It documents the rule and its rationale.
2. Run the full suite. Expect all 303 to pass (1 skipped on a bare checkout).
3. **Verify your change is actually tested.** Revert it and confirm a test fails. A test that
   passes both before and after proves nothing. The two known regressions are recorded in
   [docs/TESTING.md](docs/TESTING.md#verifying-a-test-is-non-vacuous):
   - Removing the `INVALIDATED` guard → **2 failed, 301 passed**.
   - Mapping `error` to `INVALIDATED` → **15 failed, 288 passed**.

   If your change is not caught by either, you need a new test.
4. If the change alters the contract, say so explicitly in the PR and add a `CHANGELOG.md` entry
   under the appropriate heading. Do not edit an oracle to match new behaviour without that
   discussion.

## Documentation

Documentation lives in two places: `README.md` and `docs/`. If you change behaviour, update the
document that describes it — the mapping is in the README's
[Documentation](README.md#documentation) table.

Two conventions that matter more than style:

**Verify claims against the source.** Every code snippet, error string, count and command in
`docs/` was checked against the implementation or captured from a real run. If you add a claim,
check it. Several existing claims were corrected precisely because they had not been verified — see
the `Fixed` section in [CHANGELOG.md](CHANGELOG.md).

**Document gaps, do not hide them.** Where the implementation and the manifest disagree, the docs
say so and explain the consequence. `docs/CONFIGURATION.md` and `CHANGELOG.md#known-issues` are the
model. A truthful limitation is more useful than a silent one.

Do not add documentation files that only explain a change you made. That belongs in the commit
message or the pull request description.

## Submitting a change

1. Branch from `main`. Do not commit to `main` directly.
2. Keep the change focused. A bug fix and a refactor are two pull requests.
3. Run the suite: `python -m pytest -q`. It must be **303 passed, 1 skipped** (plus whatever you add).
4. Run the validator if you have Hermes: `hermes plugins validate .` — 15/15, zero warnings.
5. If you changed the manifest, confirm `hermes plugins doctor .` still reports **6 tools, 4 hooks**
   (or the new correct count).
6. Update the docs and `CHANGELOG.md` if behaviour changed.
7. Open a pull request describing what changed and why. Include the test output.

For a security issue, do **not** open a public issue or pull request. Use the private advisory
process in [docs/SECURITY.md](docs/SECURITY.md#reporting-a-vulnerability).

## Known issues you could fix

These are real, documented, and open. Each is recorded in
[CHANGELOG.md](CHANGELOG.md#known-issues) with its impact and evidence.

| Issue | Difficulty | Note |
| --- | --- | --- |
| [KI-2](CHANGELOG.md#ki-2-checkcontextcwd-is-accepted-but-never-read) — `CheckContext.cwd` unused | Low | Either use it as the default for spec `cwd`, or remove the parameter. Either is a behaviour change. |
| [KI-4](CHANGELOG.md#ki-4-no-git-tags-or-releases) — no tags | Trivial | Tag `v0.2.2` at the pinned commit. |
| [KI-5](CHANGELOG.md#ki-5-no-continuous-integration) — no CI | Low | Add the workflow sketched in `docs/TESTING.md`. High value: nothing currently enforces the suite. |
| [KI-6](CHANGELOG.md#ki-6-db_row_count-interpolates-the-where-clause-into-sql) — `where` interpolation | Medium | Parameterising is hard for an arbitrary predicate. At minimum, validate or document more strictly. |
| [KI-9](CHANGELOG.md#ki-9-the-database-is-unencrypted-and-has-no-integrity-protection) — no encryption | Hard | Out of scope for a stdlib-only plugin, but a documented threat model is not. |
| Coverage gaps | Varies | [docs/TESTING.md](docs/TESTING.md#coverage-gaps) lists the untested paths: real HTTP, concurrency, large ledgers, WAL fallback, corruption. |

**KI-5 is the highest-value contribution** for the effort involved: a twelve-line workflow would
make every other change safer, and it is the only gap that lets a regression reach `main` silently.
