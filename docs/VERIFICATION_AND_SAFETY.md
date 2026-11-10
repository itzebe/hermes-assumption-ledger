# Verification and Safety

This is the most important document in the repository. It explains what the plugin is willing to
conclude, what it refuses to conclude, and why.

- [Deterministic verification principles](#deterministic-verification-principles)
- [The three verdicts](#the-three-verdicts)
- [The critical invariant](#the-critical-invariant)
- [State transition rules](#state-transition-rules)
- [The twelve checks](#the-twelve-checks)
- [Failure handling in detail](#failure-handling-in-detail)
- [Dependency-aware blocking](#dependency-aware-blocking)
- [Pre-proceed safety behaviour](#pre-proceed-safety-behaviour)
- [Supporting regression tests](#supporting-regression-tests)
- [What this does not guarantee](#what-this-does-not-guarantee)

## Deterministic verification principles

1. **No LLM decides a verdict.** The plugin makes zero model calls. Extraction is regular
   expressions over observable text; verification is filesystem, git, process, HTTP and SQLite
   reads. There is no prompt to argue with and no sampling to be unlucky with.
2. **The check is declared, not inferred.** An assumption carries an explicit check spec. The
   plugin never invents a test for a statement, and it never guesses that a statement is true.
3. **Observable inputs only.** The extractor reads assistant messages and tool results — text
   Hermes already exposes. Nothing inspects private model reasoning or hidden state.
4. **Conservative capture.** A sentence that matches no rule produces nothing. Fabricating an
   assumption is treated as worse than missing one.
5. **Three-valued logic.** Every check returns exactly one of `pass`, `fail`, `error`. There is no
   fourth "probably" value, and `error` is never coerced into `fail`.
6. **Verifiers are pure with respect to the ledger.** A verifier takes a spec and a context and
   returns a `CheckResult`. It performs no writes. All persistence happens in the engine, after the
   verdict is known.

## The three verdicts

Defined in `ledger/model.py`:

| Verdict | Constant | Meaning |
| --- | --- | --- |
| `pass` | `PASS` | The evidence positively supports the assumption. |
| `fail` | `FAIL` | The evidence definitively contradicts the assumption. |
| `error` | `ERROR` | Evidence could not be obtained: missing target, timeout, rate limit, provider unavailable, unsupported environment, malformed spec. |

`error` is the load-bearing one. It means *we do not know*, and the plugin is built so that
"we do not know" can never be laundered into "it is false".

## The critical invariant

> **A known contradiction must remain `INVALIDATED` when a subsequent verification attempt fails
> or becomes unavailable. Only qualifying refuting evidence may clear that invalidation.**

This is implemented in exactly one function, `status_after_check` in `ledger/model.py`:

```python
def status_after_check(previous_status, verdict, *, had_fresh_evidence):
    if verdict == PASS:
        return VALID
    if verdict == FAIL:
        return INVALIDATED
    if previous_status == INVALIDATED:
        return INVALIDATED          # <-- the invariant
    return STALE if had_fresh_evidence else UNVERIFIED
```

Read the third branch carefully. On an `error` verdict, the function checks what the record *was*
before deciding. If it was `INVALIDATED`, it stays `INVALIDATED`. The `had_fresh_evidence` fallback
that would normally produce `STALE` is never reached for a contradicted record.

### Why this matters

Without that branch, the sequence is:

1. `config/prod.json` is missing → `fail` → `INVALIDATED`
2. The decision "deploy the release" is marked `blocked`
3. The provider goes down → `error`
4. Naive logic: `error` + we held fresh evidence → `STALE`
5. `STALE` is not `INVALIDATED` → the decision is recomputed as `open`
6. **The outage silently unblocked a deployment that was already known to be unsafe**

That is precisely the class of failure the plugin exists to prevent. A network blip is not
evidence that a missing file reappeared.

### What *does* clear it

Only a `pass`. Refuting evidence must be positive evidence: the file is there, the branch matches,
the endpoint returns the expected status. `test_transient_error_does_not_soften_a_contradiction`
walks the full cycle — `VALID` → `INVALIDATED` → (outage) still `INVALIDATED`, still `blocked` →
(refuting evidence) `VALID`, `open`.

### A subtlety in the existing test coverage

`test_O10_conflicting_evidence_latest_definitive_wins` also asserts the invariant, but it reaches
it through the `unsupported` check-kind path, which returns from `LedgerEngine.verify` *before*
`status_after_check` is ever called. So O10 alone would not catch a regression in the rule. The
regression test `test_transient_error_does_not_soften_a_contradiction` was added specifically to
drive a **real** transient error through `status_after_check` with a previously-`INVALIDATED`
record. If you are reviewing a change to the lifecycle rule, that is the test to check first.

## State transition rules

The complete table, derived from `status_after_check` plus `sweep_stale`:

| Previous | Verdict | Condition | New status |
| --- | --- | --- | --- |
| any | `pass` | — | `VALID` |
| any | `fail` | — | `INVALIDATED` |
| `INVALIDATED` | `error` | — | **`INVALIDATED`** (unchanged) |
| `VALID` | `error` | held fresh evidence | `STALE` |
| `STALE` | `error` | held fresh evidence | `STALE` |
| `UNVERIFIED` | `error` | no fresh evidence | `UNVERIFIED` |
| `VALID` | — | evidence older than `freshness_seconds` | `STALE` (via `sweep_stale`) |
| `unsupported` kind | — | any | unchanged (stays `UNVERIFIED`) |

`had_fresh_evidence` is `Assumption.is_fresh(freshness_seconds)`, which is true when
`last_checked_at` exists and `now - last_checked_at <= freshness_seconds`.

Note the difference between the two ways a record becomes `STALE`:

- **Aged out.** It passed, and the evidence is now old. `sweep_stale()` degrades it. "A pass is
  never treated as permanently true."
- **Could not refresh.** It passed, and a re-check errored. `status_after_check` degrades it.
  "We still have the old evidence, we simply cannot refresh it right now."

Both mean the same thing operationally: the assumption needs re-checking before it can be relied
on again.

## The twelve checks

`SUPPORTED_KINDS` in `ledger/verifiers.py` contains exactly these twelve, sorted:

```
command_succeeds, db_row_count, db_table_exists, env_set, file_contains,
file_exists, file_hash, git_branch, git_clean, git_commit, http_json_field, http_status
```

A test asserts the registry is closed — `test_verifier_registry_is_closed` — so adding a check
requires deliberately updating that expectation.

| # | Kind | Reads | Passes when | Fails when | Errors when |
| --- | --- | --- | --- | --- | --- |
| 1 | `file_exists` | `path.exists()` | Path exists | Path does not exist | No path given; `OSError` |
| 2 | `file_contains` | File text + `re.search` | Pattern matches | Pattern absent | No path/pattern; file missing; unreadable; invalid regex |
| 3 | `file_hash` | Streamed digest | Digest equals `sha256` | Digest differs | No path/sha256; file missing; unknown algo |
| 4 | `git_branch` | `git rev-parse --abbrev-ref HEAD` | Equals `branch` | Different branch | No `branch`; git missing/timed out/non-zero exit |
| 5 | `git_commit` | `git rev-parse HEAD` | Equals or starts with `commit` | HEAD differs | No `commit`; git unavailable |
| 6 | `git_clean` | `git status --porcelain` | No *tracked* changes | Tracked changes present | git unavailable |
| 7 | `env_set` | `ctx.env` | Variable set and non-empty | Unset/empty, or differs from `equals` | No `name` |
| 8 | `command_succeeds` | Process exit code | Exit 0 | Exit non-zero | No/invalid `argv`; not found; timed out |
| 9 | `http_status` | HTTP GET | Status in `expected_status` | Status is a non-transient mismatch | Transport error; transient status |
| 10 | `http_json_field` | HTTP GET + dotted pointer | Pointer present and equals `equals` (if given) | Pointer absent; value differs; HTTP ≥ 400 | Not JSON; transient status; transport error |
| 11 | `db_table_exists` | `sqlite_master`, read-only | Table found | Table missing | No path/table; DB missing; open/query error |
| 12 | `db_row_count` | `SELECT COUNT(*)`, read-only | Within `equals`/`min`/`max` | Outside the bound | Unsafe table name; DB missing; query error |

### Notes on individual checks

**`git_commit` accepts a prefix.** `actual.startswith(expected)` means a short SHA is accepted.
`git rev-parse HEAD` always returns the full 40-character SHA, so comparing with `startswith`
lets a 7-character prefix in a check spec match.

**`git_clean` ignores untracked files by default.** This is a deliberate design decision, and the
docstring explains it: "A ledger database, a build artifact or a log file is not a change to what
was committed. Counting it would invalidate 'the tree is clean' for reasons unrelated to the
assumption." Untracked files are always returned in `observed.untracked` so they are visible in
the evidence, but they only affect the verdict when the spec sets `include_untracked: true`.

**The git checks cannot be steered into running a program.** Each `git` invocation is started with
`-c core.fsmonitor=false -c core.hooksPath=/dev/null` and the minimal environment, and `git status`
adds `--no-optional-locks`. A repository the agent merely checked out cannot therefore use its own
configuration to have git execute a `core.fsmonitor` program or a hooks-directory program during a
verification, and no host environment variable reaches git. See
[SECURITY.md](SECURITY.md#git-checks-and-repository-controlled-execution).

**`env_set` never echoes the value.** When a variable differs from `equals`, the detail is
`"environment variable differs from expected"` with only the variable name in `observed`. The
value is not recorded, because it may be a secret.

**`db_*` checks open read-only.** Both use `sqlite3.connect(f"file:{db}?mode=ro", uri=True)`.
`db_row_count` validates the table name against `[A-Za-z_][A-Za-z0-9_]*` and returns `error` for
anything else, which is what makes a spec like `{"table": "a; DROP TABLE b"}` safe — it never
reaches SQL. The `where` clause is validated against a narrow simple-predicate grammar and returns
`error` when it does not match; see [SECURITY.md](SECURITY.md#sql-predicate-validation).

**`http_json_field` uses a dotted pointer**, not RFC 6901 JSON Pointer. `a.b.0.c` walks dict keys
and list indices. A missing key or an out-of-range index returns `_MISSING`, which is reported as
a `fail` ("pointer not present in payload") — a definite answer about the payload.

## Failure handling in detail

### Transient HTTP statuses

`_TRANSIENT_HTTP = {408, 425, 429, 500, 502, 503, 504}`.

For `http_status`, a transient status produces `error` **unless it is explicitly expected**:

```python
if status in _TRANSIENT_HTTP and status not in expected_set:
    return _unknown(f"transient HTTP {status}", ...)
if status in expected_set:
    return _ok(f"HTTP {status}", ...)
return _bad(f"HTTP {status} not in {sorted(expected_set)}", ...)
```

So `expected_status: 500` is a legitimate way to assert "this endpoint is broken". And a `404` is
never transient — it is a definitive answer, so it is a `fail`.

Verified behaviour:

| Spec | Simulated response | Verdict |
| --- | --- | --- |
| `expected_status: 200` | `200` | `pass` |
| `expected_status: 200` | `404` | `fail` |
| `expected_status: 200` | `500` | `error` (transient) |
| `expected_status: 200` | `429` | `error` (transient) |
| `expected_status: 200` | `TimeoutError` raised | `error` |
| `expected_status: 200` | DNS failure | `error` |
| `expected_status: [200, 204]` | `204` | `pass` |

### Timeouts

Every check has a `default_timeout` (5.0 s from `CheckContext`) and each spec may override it with
`timeout`. Git and command checks pass it to `subprocess.run`; a `subprocess.TimeoutExpired` is
caught and returned as `error` ("git command timed out" / "command timed out"). HTTP timeouts are
passed to `urlopen` and surface as a raised exception, which is caught and returned as `error`.

A timeout is always `error`. There is no path from a timeout to `fail`.

### DNS failures

`urllib.error.URLError` (which is what a DNS failure becomes) is caught in `_default_http_get` and
returned as `{"error": str(exc.reason), "transient": True}`. `check_http_status` sees the `error`
key and returns `error` with `transient=True` in `observed`. Verified: a `dead_getter` returning
`{"error": "Name or service not known", "transient": True}` yields `verdict=error`, and the
contradiction survives.

### HTTP 429 and 5xx

Covered by the transient set above. Both are `error` when not explicitly expected.

### Unsupported verification types

Two distinct paths, both ending in "no verdict is invented":

- A check spec whose `kind` is not in `VERIFIERS` → `run_check` returns `error` with
  `"unsupported check kind"` and lists the supported kinds.
- A check spec of `{"kind": "unsupported"}` — which is what the extractor assigns to an explicit
  marker with no checkable form — is intercepted by `LedgerEngine.verify` *before* `run_check`. It
  increments `check_count`, writes an `error` evidence row reading "no deterministic check
  available for this assumption", and returns `verdict: "error"`. The status is left alone, so such
  a record stays `UNVERIFIED` indefinitely.

The second path is deliberate: the agent said something it believes, we cannot test it, and we
record that honestly rather than dropping the statement.

### Stale evidence

`Assumption.is_fresh(freshness_seconds)` compares `now - last_checked_at` to the window. A `VALID`
record that is not fresh is degraded by `sweep_stale()`, which is called from `summary()` and from
`on_pre_verify`. Degrading writes an `error` evidence row explaining the window, so the reason is
in the audit trail rather than only in the status column.

### Malformed input

Every malformed input path returns `error`, verified directly:

| Input | Verdict |
| --- | --- |
| `{"kind": "nope"}` | `error` — unsupported check kind |
| `{"kind": None}` | `error` — unsupported check kind |
| `{}` | `error` — unsupported check kind |
| `{"kind": "file_exists"}` (no path) | `error` — no path supplied |
| `{"kind": "db_row_count", "table": "a; DROP TABLE b"}` | `error` — unsafe table name |
| `None` / `"not-a-dict"` | `error` — check spec must be a mapping |

And the extractor never raises: `""`, `"   "`, `"\x00\x01"`, `"ASSUMPTION:"`, `"http://"` and
`"commit "` all return an empty list (`test_O11b_extractor_never_raises_on_garbage`).

## Dependency-aware blocking

A decision is `blocked` **iff** at least one dependency is `INVALIDATED`:

```python
def _recompute_decision_block(self, dec):
    broken = [asm for asm in dec.depends_on
              if (a := self.store.get_assumption(asm)) and a.status == INVALIDATED]
    if broken:
        dec.state = "blocked"
        dec.blocked_reason = f"depends on invalidated assumption(s): {names}"
    else:
        dec.state = "open"
        dec.blocked_reason = None
```

Three properties follow, all of them intentional:

1. **Only invalidated dependencies block.** `VALID`, `STALE` and `UNVERIFIED` dependencies do not
   block. An unverified assumption is a reason to check, not a reason to halt.
2. **Unrelated decisions are never touched.** Only decisions that name the assumption in
   `depends_on` are recomputed.
3. **Blocking is reversible.** Because `_recompute_decision_block` runs on every outcome, a
   repaired assumption unblocks the decision. `test_decision_recovers_when_assumption_repaired`
   covers this.

`critical_blockers(session_id=...)` narrows this to what a gate should act on: `INVALIDATED`
assumptions that have at least one dependent decision *in the given session*. An invalidated
assumption nobody depends on is *not* a blocker, so it never stops a turn, and a decision recorded
in another session is not this session's concern.

## Pre-proceed safety behaviour

Two gates, both conditional on a real blocker existing.

### `pre_llm_call`

Runs once per turn, before inference. Three possible outcomes:

| Condition | Return | Effect |
| --- | --- | --- |
| A decision *in this session* depends on an `INVALIDATED` assumption | `{"context": ...}` | A warning is prepended to the injected context. The turn proceeds. |
| No blocker, but live assumptions exist | `{"context": ...}` | The compact assumption block is injected into the user message. |
| No blocker, no live assumptions | `None` | The gate is inert. |
| Any exception | `None` (logged) | Fails open. |

`pre_llm_call` never returns `{"action": "block"}`. Stopping the turn before inference would prevent
the agent from investigating and clearing the problem, and would stop it in every session. The one
hard gate is `pre_verify`.

Real output of the warning path, captured from the running plugin:

```
Assumption Ledger warning: work recorded in this session depends on an assumption that evidence has contradicted.
- INVALIDATED: the file missing.txt exists (id asm_4097c971c7d0)
  relied on by: consume missing.txt
Re-check the assumption or change the plan before continuing.
```

And the context path:

```
Assumption Ledger — operational assumptions currently in effect:
  [VALID] the file app.py exists  (id asm_4097c971c7d0)
  [UNVERIFIED] the file config/prod.json exists  (id asm_2199bf66ac95)
```

Hermes injects `context` into the **user message**, never the system prompt.

### `pre_verify`

Runs once per turn when the agent edited code and is about to verify or finish. It first calls
`sweep_stale()`, then checks for blockers:

| Condition | Return | Effect |
| --- | --- | --- |
| A blocker exists | `{"action": "continue", "message": ...}` | The turn is held so the contradiction is addressed. |
| No blocker | `None` | The turn finishes normally. |

Hermes bounds how many times this can hold a turn (`agent.max_verify_nudges`), so the gate cannot
loop forever.

## Supporting regression tests

The safety properties are backed by named tests. Run them with:

```bash
python -m pytest tests/test_frozen_oracles.py -v
```

| Property | Test |
| --- | --- |
| A contradiction survives a transient error | `test_transient_error_does_not_soften_a_contradiction` |
| Uncertainty never invalidates | `test_uncertainty_never_invalidates` |
| Conflicting evidence: latest definitive wins | `test_O10_conflicting_evidence_latest_definitive_wins` |
| Missing evidence is `UNVERIFIED` | `test_O3_missing_evidence_is_unverified` |
| Provider timeout is `UNVERIFIED` | `test_O4_provider_timeout_is_unverified` |
| Rate limit is `UNVERIFIED` | `test_O5_rate_limited_provider_is_unverified` |
| 5xx is `UNVERIFIED`, not `fail` | `test_O5b_server_error_is_unverified` |
| Invalidation blocks dependents | `test_O7_invalidation_blocks_dependent_decision` |
| Unrelated decisions stay open | `test_O7b_unrelated_decision_is_not_blocked` |
| Propagation reaches only dependents | `test_invalidation_propagates_only_to_dependents` |
| A repaired assumption unblocks | `test_decision_recovers_when_assumption_repaired` |
| Malformed specs are `ERROR` | `test_O11_malformed_extraction_output` |
| The gate is inert without a dependent decision | `test_pre_llm_warns_but_never_blocks` |
| The gate is scoped to the affected session | `test_pre_verify_holds_only_the_affected_session` |
| An unrelated session continues | `test_unrelated_session_is_never_blocked` |
| A re-check clears a block | `test_resolve_pass_sets_valid_and_clears_the_block` |
| A drop records no pass | `test_drop_records_no_pass_and_marks_dropped` |
| An outage keeps a contradiction | `test_transient_failure_does_not_clear_a_contradiction` |
| Hooks never raise on hostile input | `test_hooks_never_raise_on_hostile_input` |

### Are these tests non-vacuous?

They are meant to fail when the rule they protect is broken. The oracle table is *frozen*: the
docstring in `tests/test_frozen_oracles.py` states that a failing oracle means the change is wrong
until proven otherwise, and that expected values must not be edited to make a test pass.

This was checked, not assumed. There are two ways to break the uncertainty rule, and they fail
different numbers of tests.

**Removing the `INVALIDATED` guard** — deleting the
`if previous_status == INVALIDATED: return INVALIDATED` branch, so a failed re-check degrades a
contradiction to `STALE` — makes **two** tests fail:

```
tests/test_frozen_oracles.py::test_transient_error_does_not_soften_a_contradiction
  AssertionError: a transient error must not soften a contradiction
  assert 'STALE' == 'INVALIDATED'
tests/test_recovery.py::test_drop_is_not_resurrected_by_an_error_recheck

2 failed, 305 passed
```

**Mapping `error` to `INVALIDATED`** (the naive rule) fails fourteen:

```
14 failed, 293 passed
```

Two observations worth carrying forward:

- **The protection is real but narrow.** Two tests guard the invariant against the outage case.
  That is sufficient to detect a regression, but it means a reviewer changing the lifecycle rule
  must run the suite rather than reasoning about the change in isolation.
- **`test_uncertainty_never_invalidates` does *not* catch the guard regression.** It starts from
  `UNVERIFIED`, where the guard is irrelevant, so it passes when the guard is removed. It catches
  the other direction only. Do not count it as coverage for the invariant.

If you change the lifecycle rule, verify the relevant test actually fails when you revert your
change. A test that passes both before and after proves nothing.

## What this does not guarantee

Being explicit about the limits matters more than the marketing.

- **It does not make an agent safe.** It makes *one class* of mistake visible: a belief the agent
  stated, which a deterministic check can test, which has been contradicted, and which something
  depends on. Everything else is out of scope.
- **It does not detect unstated assumptions.** If the agent never says "the file exists" and never
  writes an `ASSUMPTION:` marker, the plugin has nothing to capture. Extraction is conservative by
  design, which means it misses things.
- **It does not prove an assumption true in general.** A `pass` means one check, at one moment, saw
  the expected evidence. It is not a proof, and freshness exists precisely because it stops being
  true.
- **It cannot test what has no deterministic check.** Those records stay `UNVERIFIED` forever,
  honestly labelled.
- **Its `command_succeeds` check runs whatever the spec says.** It is documented as a read-only
  probe, but the plugin does not enforce that. See
  [SECURITY.md](SECURITY.md#command_succeeds-runs-an-arbitrary-argv).
- **It does not guarantee a turn is stopped.** Hermes caps verify-gate nudges; a determined agent
  can still act. The gate raises the cost of ignoring a contradiction; it does not make it
  impossible.
- **Nothing here has been security-audited.** See [SECURITY.md](SECURITY.md#audit-status).
