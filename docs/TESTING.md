# Testing

How to run the tests, what they cover, and — importantly — what they do not.

- [Quick start](#quick-start)
- [Test suite overview](#test-suite-overview)
- [Running the tests](#running-the-tests)
- [Test inventory](#test-inventory)
- [The frozen oracle table](#the-frozen-oracle-table)
- [Hermes-level validation](#hermes-level-validation)
- [Writing new tests](#writing-new-tests)
- [Verifying a test is non-vacuous](#verifying-a-test-is-non-vacuous)
- [Coverage gaps](#coverage-gaps)
- [Continuous integration](#continuous-integration)

## Quick start

```bash
pip install -e ".[dev]"
python -m pytest -q
```

Expected: **307 passed, 1 skipped**, in a few seconds. (The skip is the live-core comparison in
`test_core_parity.py`; it runs only inside a Hermes checkout.)

```
........................................................................ [ 23%]
........................................................................ [ 46%]
........................................................................ [ 70%]
........................................................................ [ 93%]
....................                                                     [100%]
307 passed, 1 skipped in 2.30s
```

The suite is deterministic. Consecutive runs produce the same 307 passes, with no order-dependence,
no skips, no xfails, and no network access. HTTP checks are exercised through injected transports
and resolver stubs, so nothing leaves the machine.

## Test suite overview

| File | Tests | Lines | Focus |
| --- | --- | --- | --- |
| `tests/test_frozen_oracles.py` | 17 | 331 | The adversarial scenario table. The contract. |
| `tests/test_verifiers.py` | 34 | 430 | Each of the 12 check kinds, plus failure modes and git hardening. |
| `tests/test_engine.py` | 14 | 194 | Capture, verification, propagation, freshness, reporting. |
| `tests/test_extractor.py` | 12 | 105 | Extraction precision — including what must *not* be extracted. |
| `tests/test_plugin_integration.py` | 12 | 251 | The Hermes adapter: hooks, tools, slash command, manifest. |
| `tests/test_ssrf.py` | 67 | 380 | The SSRF policy: schemes, loopback, private, metadata, redirects, DNS. |
| `tests/test_recovery.py` | 26 | 342 | Session-scoped blocking, `resolve`, `drop`, and the error invariant. |
| `tests/test_injection.py` | 13 | 173 | Tool output is never captured; planted markers create nothing. |
| `tests/test_config.py` | 33 | 287 | Every declared setting has an observable effect. |
| `tests/test_command_policy.py` | 41 | 204 | Allowlist parsing, path refusal, resolution, minimal environment. |
| `tests/test_core_parity.py` | 39 | 106 | SSRF verdicts match Hermes core's `is_safe_url`. |
| **Total** | **307 (+1 skip)** | **2803** | |

The split is deliberate. `test_frozen_oracles.py` encodes *what the product promises*; the other
files test the machinery that delivers it, with the four security-focused files covering the
threats a maintainer would ask about.

## Proving the security tests are not vacuous

A test that passes with and without its fix proves nothing. `scripts/nonvacuous_check.py` reverts
one hardening change at a time, runs the focused suites, and restores the file:

```bash
python3 scripts/nonvacuous_check.py
```

Every one of the 20 reverts must be caught. If any revert leaves the suite green, that test is
reported as `VACUOUS!` and the script exits non-zero.

## Running the tests

```bash
# Everything
python -m pytest

# One file
python -m pytest tests/test_frozen_oracles.py

# One test, verbose
python -m pytest tests/test_frozen_oracles.py::test_transient_error_does_not_soften_a_contradiction -v

# Stop at the first failure
python -m pytest -x

# Show the oracle scenario names
python -m pytest tests/test_frozen_oracles.py -v

# The tests that matter most for the safety invariant
python -m pytest tests/test_frozen_oracles.py -k "transient or uncertain or conflicting or O10" -v
```

There is no `conftest.py`, and no plugin requirement beyond pytest itself. Test discovery is
configured in `pyproject.toml`:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"
```

Every test that needs a filesystem or a clock uses pytest's `tmp_path` and an injected clock, so
nothing writes outside the temporary directory and nothing sleeps.

## Test inventory

### `test_frozen_oracles.py` — the contract

An oracle table mapping a scenario to its expected outcome. Names are prefixed `O<n>`.

| Test | Scenario |
| --- | --- |
| `test_O1_correct_assumption_with_evidence` | A true assumption with confirming evidence becomes `VALID`. |
| `test_O2_false_assumption_with_contradictory_evidence` | A false assumption with contradicting evidence becomes `INVALIDATED`. |
| `test_O3_missing_evidence_is_unverified` | No evidence yet → `UNVERIFIED`. |
| `test_O4_provider_timeout_is_unverified` | A timeout → `UNVERIFIED`, never `INVALIDATED`. |
| `test_O5_rate_limited_provider_is_unverified` | A rate limit → `UNVERIFIED`. |
| `test_O5b_server_error_is_unverified` | A 5xx → `UNVERIFIED`. |
| `test_O6_evidence_becomes_stale` | Aged-out evidence → `STALE`. |
| `test_O7_invalidation_blocks_dependent_decision` | Invalidation blocks a dependent decision. |
| `test_O7b_unrelated_decision_is_not_blocked` | An unrelated decision is untouched. |
| `test_O8_restart_and_persistent_recovery` | Close and reopen the store; records survive. |
| `test_O9_compaction_reinjection` | After total in-memory loss, re-injection still works. |
| `test_O10_conflicting_evidence_latest_definitive_wins` | The latest definitive verdict wins. |
| `test_O11_malformed_extraction_output` | Malformed specs produce `ERROR`, never a fabricated verdict. |
| `test_O11b_extractor_never_raises_on_garbage` | The extractor never raises. |
| `test_O12_duplicate_assumption_detection` | Re-observing merges instead of duplicating. |
| `test_transient_error_does_not_soften_a_contradiction` | **The invariant.** A contradiction survives an outage. |
| `test_oracle_table_is_complete` | Meta-test: every oracle scenario has a test. |

### `test_verifiers.py` — the twelve checks

Covers each kind in both directions (pass and fail), plus the error paths: missing fields,
unreadable files, non-zero exits, transient HTTP statuses, injected HTTP failures, and the unsafe
table-name guard. HTTP is exercised through an injected `http_get`, so there is no network access.

### `test_engine.py` — engine behaviour

| Area | Representative tests |
| --- | --- |
| Capture | `test_uncertainty_never_invalidates`, `test_stale_then_revalidated` |
| Propagation | `test_invalidation_propagates_only_to_dependents`, `test_decision_recovers_when_assumption_repaired` |
| Reporting | Summary shape, render functions, `critical_blockers` |
| Freshness | Aged evidence degrades to `STALE`; a fresh pass restores `VALID` |

### `test_extractor.py` — extraction precision

Half of this file tests what must **not** be extracted. That is the point: the extractor's value is
its conservatism.

| Test | Asserts |
| --- | --- |
| `test_tool_result_harvests_nothing_at_all` | Tool output yields nothing, not even explicit markers. |
| Prose rejection | `"I think we are probably fine."` extracts nothing. |
| Dedupe | A duplicated sentence yields one record. |
| Malformed input | Empty, whitespace and control characters extract nothing and do not raise. |

### `test_plugin_integration.py` — the Hermes adapter

| Test | Asserts |
| --- | --- |
| Manifest consistency | Declared tools and hooks match what `register()` registers. |
| `test_pre_llm_warns_but_never_blocks` | `pre_llm_call` returns context, never a block action. |
| `test_pre_verify_holds_only_the_affected_session` | The hard gate is session-scoped. |
| `test_hooks_never_raise_on_hostile_input` | Every hook tolerates junk payloads. |
| Tool handlers | Each returns valid JSON, including on error. |
| Slash command | Argument parsing, including the unknown-subcommand usage message. |
| Storage isolation | `HERMES_HOME` is redirected so tests never touch a real profile. |

### `test_ssrf.py` — the HTTP policy

67 cases covering schemes (`ftp`, `file`, `data`, `gopher`, `ws`), loopback (including `[::1]` and
`127.1.2.3`), RFC1918 and CGNAT ranges, link-local, cloud-metadata endpoints (AWS, GCP, Alibaba,
and an IPv4-mapped IPv6 form), DNS rebinding (a public name answered with a private address),
ambiguous authorities (userinfo, backslash, percent-encoding, invalid port), DNS failure, and
redirect handling (re-validation, loops, and a public hop that is followed). Every refusal must
surface as `error`, never `fail`.

### `test_recovery.py` — blocking, recovery and the error invariant

The six behaviours a reviewer asks about: an invalidated assumption blocks its dependent decision;
an unrelated session continues; the affected session can inspect; a verified resolution clears the
block; a `drop` records no pass; and a transient failure does not erase a known contradiction.

### `test_injection.py` — tool output is not a capture source

Planted `ASSUMPTION:` markers in web pages, file listings and terminal output must create nothing
and must never appear in re-injected context. Repeated injection attempts accumulate nothing.

### `test_config.py` — every declared setting is wired

Each option is set and an observable effect asserted, so a setting that stops being read fails the
suite. Includes a manifest-vs-code agreement check and a version-agreement check.

### `test_command_policy.py` — the execution policy in isolation

Allowlist parsing (string and list forms, dedupe, bounds), refusal of paths and shell
metacharacters, resolution of a bare name to an absolute path, and the minimal child environment.

### `test_core_parity.py` — the SSRF policy matches core

A frozen table of the verdict core's `tools.url_safety.is_safe_url` returns for 35 URLs. The table
is checked on every run, so it works on a bare plugin checkout; when a Hermes checkout is
importable the table is regenerated live and compared. `scripts/core_parity.py <hermes-checkout>`
reproduces the comparison against a real core and prints the commit it verified. Verified at core
`ff6781e8e1`: 35 cases, 0 mismatches.

## The frozen oracle table

`tests/test_frozen_oracles.py` opens with a docstring stating the rule. It is worth quoting, because
it governs how you are expected to treat a failure:

> The point of an oracle is that it is frozen: if a later change makes one of these fail, the
> change is wrong until proven otherwise. Do not edit an expected value to make a test pass;
> fix the engine instead.

The practical consequence: **a failing oracle is a design conversation, not a test to fix.** The
oracles encode the promises in [VERIFICATION_AND_SAFETY.md](VERIFICATION_AND_SAFETY.md). Changing
one means changing the product's contract, which is a decision to make deliberately and to record
in [CHANGELOG.md](../CHANGELOG.md).

`test_oracle_table_is_complete` is a meta-test: it checks that every scenario named in the oracle
table has a corresponding test function. Adding a scenario without a test fails the suite.

## Hermes-level validation

The Python tests exercise the plugin's own logic. They do not prove that **Hermes** accepts the
plugin. Two upstream commands do that, and both were run against this repository:

### `hermes plugins validate <path>`

Runs 15 checks. All pass, with zero warnings:

| Check | Result |
| --- | --- |
| manifest | `plugin.yaml parses` |
| manifest fields | `name, version, description present` |
| requires_hermes | `spec '>=0.23.0' parses` |
| config schema | `shape valid` |
| requires_env | `all entries UPPER_SNAKE` |
| oauth | `not declared` |
| loadable | `entry: __init__.py` |
| python dependencies | `none declared` |
| capability probe | `register() ran in isolation` |
| declared tools | `matches registrations` |
| declared hooks | `matches registrations` |
| declared middleware | `matches registrations` |
| built-in tool collisions | `no collisions` |
| security scan | `safe` |
| no core override | `no runtime rebinds of Hermes core` |

The `capability probe` is the substantive one: Hermes imports the plugin in isolation, runs
`register()`, and compares the result against the manifest. This is what catches a manifest that
has drifted from the code.

### `hermes plugins doctor <path>`

```
Plugin Doctor: <path>
  manifest: assumption-ledger 0.2.2 (standalone)
  OK: runtime discovery, manifest parsing, import, and registration passed
  registrations: 6 tool(s), 4 hook(s)
```

**6 tools, 4 hooks** is the expected count. Fewer means something failed to register.

Neither command is part of `pytest`; run them explicitly. `doctor` supports `--ci` to exit non-zero
when validation reports an error, which is the right flag for a pipeline.

## Writing new tests

Conventions used throughout the suite:

**Use the real engine, not mocks.** Every test constructs a real `LedgerStore` over a real SQLite
file in `tmp_path` and a real `LedgerEngine`. There is no mocking framework in use.

**Inject at the seam, not by patching.** To control HTTP, pass an `http_get` callable into
`CheckContext`. To control the clock, pass `now` into `LedgerEngine`. To control process execution,
pass a `runner`. These are constructor parameters, so no monkeypatching is needed:

```python
def _engine(tmp_path, http_get=None):
    store = LedgerStore(tmp_path / "ledger.db")
    return LedgerEngine(store, ctx=CheckContext(root=tmp_path, cwd=tmp_path, http_get=http_get))

def test_provider_timeout_is_uncertainty(tmp_path):
    def boom(url, timeout):
        raise TimeoutError("no route")
    eng = _engine(tmp_path, http_get=boom)
    a = eng.add_assumption("the endpoint https://x/ is reachable",
                           {"kind": "http_status", "url": "https://x/", "expected_status": 200})
    r = eng.verify(a.id)
    assert r["verdict"] == "error"
    assert r["status"] == "UNVERIFIED"
```

**Isolate the Hermes home.** Any test touching the adapter should redirect storage:

```python
monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
```

Without this, a test can write into a real profile's `plugin-data/` directory.

**For a new oracle, add both the scenario and the test.** `test_oracle_table_is_complete` will fail
if you add one without the other.

## Verifying a test is non-vacuous

A test that passes before and after a change proves nothing. The way to check is to revert the
behaviour and confirm the test fails.

The safety invariant was checked this way, and the result is worth stating precisely. There are two
ways to break the uncertainty rule, and they fail different numbers of tests:

**Break 1 — remove the `INVALIDATED` guard.** Deleting the
`if previous_status == INVALIDATED: return INVALIDATED` branch, so a failed re-check degrades a
contradiction to `STALE`:

```
tests/test_frozen_oracles.py::test_transient_error_does_not_soften_a_contradiction
  AssertionError: a transient error must not soften a contradiction
  assert 'STALE' == 'INVALIDATED'
tests/test_recovery.py::test_drop_is_not_resurrected_by_an_error_recheck

2 failed, 305 passed
```

**Break 2 — make `error` map to `INVALIDATED`** (the naive rule, where any failed check is treated
as a refutation):

```
14 failed, 293 passed

FAILED tests/test_config.py::test_command_allowlist_enables_command_checks
FAILED tests/test_config.py::test_allow_private_urls_changes_http_policy
FAILED tests/test_config.py::test_allow_private_urls_does_not_unblock_metadata
FAILED tests/test_engine.py::test_uncertainty_never_invalidates
FAILED tests/test_frozen_oracles.py::test_O3_missing_evidence_is_unverified
FAILED tests/test_frozen_oracles.py::test_O4_provider_timeout_is_unverified
FAILED tests/test_frozen_oracles.py::test_O5_rate_limited_provider_is_unverified
FAILED tests/test_frozen_oracles.py::test_O5b_server_error_is_unverified
... plus the six test_error_paths_never_invalidate cases
```

Three lessons:

- **The invariant is protected, but narrowly.** Break 1 fails two tests: the oracle for the outage
  case and the recovery test that a `drop` is not resurrected. That is enough to catch the
  regression, and it means you must actually run the suite when you touch
  `status_after_check` rather than reasoning about the change.
- **Do not assume a related test covers it.** `test_uncertainty_never_invalidates` passes under
  break 1, because it starts from `UNVERIFIED`, where the guard never applies. It only catches
  break 2. The two directions of the rule have separate, non-overlapping coverage.
- **The counts are the evidence.** If you are auditing this suite, reproduce both numbers yourself
  before trusting the claim that the invariant is tested.

Do the same for anything you add to the safety path.

## Coverage gaps

Being explicit about what the suite does **not** cover:

| Gap | Detail |
| --- | --- |
| Real network HTTP | All HTTP tests inject `http_get`. No test makes a real outbound request, so `urllib` integration and real TLS behaviour are untested. |
| Real Hermes runtime | Hooks are called directly as functions. They are not driven through a live Hermes session, so the real payload shapes are asserted from the documented contract rather than observed end-to-end. |
| `hermes plugins validate` in CI | Run manually. It is not part of the pytest suite, because it needs a Hermes installation. |
| Concurrency | The store's lock is exercised only single-threaded. No test drives simultaneous hook and tool writes. |
| Large ledgers | No test covers behaviour with thousands of assumptions, or the 25-item re-injection cap being hit. |
| Windows paths | Tests assume POSIX path semantics. |
| WAL fallback | The `DELETE`-journal fallback for filesystems without WAL support is not exercised. |
| Corruption | A corrupt `ledger.db` is not tested. |
| Freshness at real time boundaries | The clock is injected, so the exact boundary comparison is not tested against wall-clock drift. |

If you are extending the suite, these are the highest-value gaps.

## Continuous integration

**There is no CI configuration in this repository.** There is no `.github/workflows/` directory.
Tests are run locally.

That is a real limitation rather than a design choice, and it is recorded here so it is not mistaken
for coverage. A minimal workflow would be:

```yaml
name: tests
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install -e ".[dev]"
      - run: python -m pytest -q
```

The plugin's zero runtime dependencies make this cheap to add — nothing needs to be installed
beyond pytest. See [CONTRIBUTING.md](../CONTRIBUTING.md) if you want to contribute one.
