# API Reference

Every public interface the plugin exposes to Hermes, plus the internal Python API you can use
directly. Everything here was read from the source and exercised against the running plugin.

- [Registered tools](#registered-tools)
- [Registered hooks](#registered-hooks)
- [Slash command](#slash-command)
- [Python API: LedgerEngine](#python-api-ledgerengine)
- [Python API: LedgerStore](#python-api-ledgerstore)
- [Python API: verifiers](#python-api-verifiers)
- [Python API: extractor](#python-api-extractor)
- [Python API: model](#python-api-model)

## Registered tools

Four tools, all in the toolset `assumption_ledger`. Every handler returns a **JSON string** —
including on error, where the string is `{"error": "..."}`. No handler raises.

### `assumption_record`

Record one operational assumption together with the deterministic check that will test it.

| Argument | Type | Required | Description |
| --- | --- | --- | --- |
| `statement` | string | yes | The assumption in plain language. Whitespace-only is rejected. |
| `check` | object | yes | Check spec. Must be a mapping with a `kind`. |

**Returns** on success: `{"id": ..., "status": ..., "statement": ...}`. Newly recorded
assumptions always start `UNVERIFIED` — recording is not verifying.

**Errors**: `{"error": "statement is required"}`, `{"error": "check must be a mapping with a
'kind'"}`, `{"error": "could not record: ..."}`.

**Side effects**: inserts or merges an `assumptions` row. Recording the same statement with the
same check twice increments `seen_count` rather than creating a second record.

Real invocation and output:

```
assumption_record({"statement": "the file app.py exists",
                   "check": {"kind": "file_exists", "path": "/tmp/ledger-work-fk4ngsxu/app.py"}})

{"id": "asm_4097c971c7d0", "status": "UNVERIFIED", "statement": "the file app.py exists"}
```

Bad input:

```
assumption_record({"statement": "x"})
  {"error": "check must be a mapping with a 'kind'"}

assumption_record({"check": {"kind": "file_exists"}})
  {"error": "statement is required"}
```

### `assumption_check`

Verify one assumption by id, or every assumption when `id` is omitted.

| Argument | Type | Required | Description |
| --- | --- | --- | --- |
| `id` | string | no | Assumption id. Omit to check all. |

**Returns** with an id: the engine's result dict —
`{"assumption", "status", "verdict", "detail", "observed"}`. Without an id:
`{"results": [ ...one result dict per assumption... ]}`.

**Errors**: `{"error": "check failed: ..."}`; for an unknown id,
`{"assumption": ..., "status": ..., "verdict": ..., "detail": ...}` is not returned — instead the
engine returns `{"error": "unknown assumption <id>"}`.

Real invocation and output:

```
assumption_check({"id": "asm_4097c971c7d0"})

{"assumption": "asm_4097c971c7d0", "status": "VALID", "verdict": "pass",
 "detail": "path exists", "observed": {"path": "/tmp/ledger-work-fk4ngsxu/app.py"}}
```

**Side effects**: writes an `evidence` row, updates the assumption's status and counters, and
recomputes the blocked state of every dependent decision. This is the tool with real consequences
— it can invalidate an assumption and block work.

### `assumption_status`

Return the ledger summary. Takes no arguments.

**Returns**: the `summary()` dict — `total`, `by_status` (all four keys always present),
`invalidated`, `unverified`, `stale`, `blocked_decisions`, `supported_check_kinds`.

**Side effects**: calls `sweep_stale()` first, so calling status can degrade aged-out `VALID`
records to `STALE`.

Real output (abridged — the real string is one line):

```json
{
  "total": 1,
  "by_status": {"VALID": 1, "INVALIDATED": 0, "UNVERIFIED": 0, "STALE": 0},
  "invalidated": [],
  "unverified": [],
  "stale": [],
  "blocked_decisions": [],
  "supported_check_kinds": [
    "command_succeeds", "db_row_count", "db_table_exists", "env_set",
    "file_contains", "file_exists", "file_hash", "git_branch", "git_clean",
    "git_commit", "http_json_field", "http_status"
  ]
}
```

### `assumption_decide`

Record a plan step or action and the assumptions it depends on.

| Argument | Type | Required | Description |
| --- | --- | --- | --- |
| `statement` | string | yes | The plan step or action. |
| `depends_on` | array of strings | no | Assumption ids this decision relies on. Defaults to `[]`. |

**Returns**: `{"id": ..., "state": ..., "blocked_reason": ...}`. `state` is `"open"` or
`"blocked"`, computed immediately from the current status of the dependencies.

**Errors**: `{"error": "statement is required"}`, `{"error": "depends_on must be a list of
assumption ids"}`.

**Side effects**: inserts or merges a `decisions` row and computes its blocked state.

Real invocation and output:

```
assumption_decide({"statement": "run app.py", "depends_on": ["asm_4097c971c7d0"]})
  {"id": "dec_efb83d09cca1", "state": "open", "blocked_reason": null}

assumption_decide({"statement": "x", "depends_on": "nope"})
  {"error": "depends_on must be a list of assumption ids"}
```

## Registered hooks

Four hooks, all members of Hermes' `VALID_HOOKS`. Callbacks are wrapped in `try/except` and log on
failure, so a plugin bug fails open rather than breaking the agent loop.

### `pre_llm_call`

Fires once per turn before inference. Receives Hermes' standard kwargs (`session_id`, `task_id`,
`turn_id`, `user_message`, `conversation_history`, `is_first_turn`, `model`, `platform`,
`parent_session_id`, `sender_id`); this plugin reads none of them.

| Condition | Return | Hermes effect |
| --- | --- | --- |
| A decision depends on an `INVALIDATED` assumption | `{"action": "block", "message": str}` | Turn ends before inference. |
| No blocker, live assumptions exist | `{"context": str}` | Injected into the **user message**. |
| No blocker, nothing live | `None` | Inert. |
| Exception | `None` | Fails open. |

Real block message:

```
Assumption Ledger stopped this turn: a decision depends on an assumption that evidence has contradicted.
- INVALIDATED: the file missing.txt exists (reason: path does not exist)
  blocks decision: consume missing.txt
Re-check the assumption or change the plan before continuing.
```

Real context block:

```
Assumption Ledger — operational assumptions currently in effect:
  [VALID] the file app.py exists  (id asm_4097c971c7d0)
  [UNVERIFIED] the file config/prod.json exists  (id asm_2199bf66ac95)
```

When blockers exist the function returns `{"action": "block"}` and **not** the context block; the
two are mutually exclusive.

### `post_llm_call`

Fires after the model responds. Reads `assistant_response`; also records `session_id`, `turn_id`
and `model` into the assumption's provenance when present.

**Returns** `None` (observer). **Side effects**: extracts assumptions from the reply — markers and
natural-language patterns — and stores them. A falsy `assistant_response` is ignored.

### `post_tool_call`

Fires after every tool call. Reads `result` (dicts are JSON-encoded first) and `tool_name`; records
`tool_call_id` and `session_id` into provenance.

**Returns** `None` (observer). **Side effects**: extracts **explicit markers only** from the result
text, capped at the first 20000 characters. Natural-language patterns are deliberately not applied
to tool output — see [ARCHITECTURE.md](ARCHITECTURE.md#assumption-capture-flow).

### `pre_verify`

Fires once per turn when the agent edited code and is about to verify or finish. Receives
`session_id`, `platform`, `model`, `coding`, `attempt`, `final_response`, `changed_paths`; this
plugin reads none of them. It calls `sweep_stale()` first.

| Condition | Return | Hermes effect |
| --- | --- | --- |
| A blocker exists | `{"action": "continue", "message": str}` | Turn is held. |
| No blocker | `None` | Turn finishes normally. |
| Exception | `None` | Fails open. |

Hermes bounds how many times this can hold a turn, so the gate cannot loop forever.

## Slash command

### `/assumptions`

Registered via `register_command("assumptions", handler, description=..., args_hint="[status|list|check [id]|show <id>]")`.
The handler signature is `command_assumptions(raw_args: str = "", **kwargs) -> str`.

| Invocation | Behaviour |
| --- | --- |
| `/assumptions` | Status view (`render_status`). |
| `/assumptions status` | Same as above. |
| `/assumptions summary` | Same as above. |
| `/assumptions list` | One line per assumption: `[STATUS] id statement`. |
| `/assumptions check` | Verifies **all** assumptions, then prints the status view. |
| `/assumptions check <id>` | Verifies one assumption, prints the result as indented JSON. |
| `/assumptions show <id>` | Detail view for one assumption. |
| anything else | `Usage: /assumptions [status|list|check [id]|show <id>]` |

Real outputs:

```
$ /assumptions
Assumption Ledger
  tracked: 1  valid=1  invalidated=0  unverified=0  stale=0
  live assumptions (in effect):
    - [VALID] asm_4097c971c7d0 the file app.py exists

$ /assumptions list
[VALID] asm_4097c971c7d0 the file app.py exists

$ /assumptions show asm_4097c971c7d0
asm_4097c971c7d0  [VALID]
  statement: the file app.py exists
  check: {'kind': 'file_exists', 'path': '/tmp/ledger-work-fk4ngsxu/app.py'}
  provenance: tool (extractor=tool)
  created: 2026-11-06 20:55:56
  checks: 1  fails: 0  seen: 2
  dependent decisions:
    - dec_efb83d09cca1 [open] run app.py
  evidence (newest first):
    - pass: path exists

$ /assumptions bogus-subcommand-xyz
Usage: /assumptions [status|list|check [id]|show <id>]
```

Note the argument parsing: a bare `check` verifies everything, but `check <id>` verifies one. The
split is `arg.split(maxsplit=1)`, so `/assumptions show  asm_x` (extra space) still resolves
because the id is stripped.

## Python API: LedgerEngine

```python
from ledger import LedgerEngine, LedgerStore, CheckContext

store = LedgerStore("ledger.db")
engine = LedgerEngine(store, freshness_seconds=86400,
                      ctx=CheckContext(root=".", cwd="."))
```

Constructor parameters:

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `store` | `LedgerStore` | required | Positional. |
| `freshness_seconds` | float | `86400` (`DEFAULT_FRESHNESS_SECONDS`) | Keyword-only. |
| `ctx` | `CheckContext` | `CheckContext()` | Keyword-only. |
| `now` | callable | `time.time` | Keyword-only. Injectable clock. |

### Capture

| Method | Returns | Notes |
| --- | --- | --- |
| `observe_message(text, *, origin="assistant", meta=None)` | `list[Assumption]` | Markers + patterns. |
| `observe_tool_result(tool_name, result, meta=None)` | `list[Assumption]` | Markers only. |
| `add_assumption(statement, check, *, origin="manual", meta=None)` | `Assumption` | One record. |

`meta` is merged into the record's provenance. The plugin passes `session_id`, `turn_id`, `model`
or `tool_call_id` there.

### Decisions

| Method | Returns | Notes |
| --- | --- | --- |
| `record_decision(statement, depends_on=None, *, origin="manual")` | `Decision` | Computes blocked state immediately. |
| `link_decision(decision_id, assumption_ids)` | `Decision` or `None` | Adds dependencies; `None` if the decision is unknown. |

### Verification

| Method | Returns | Notes |
| --- | --- | --- |
| `verify(asm_id)` | `dict` | `{"error": ...}` for an unknown id. |
| `verify_all(statuses=None)` | `list[dict]` | `statuses` filters by *current* status. |

Result dict shape: `{"assumption", "status", "verdict", "detail", "observed"}`. The
`unsupported` check-kind path returns `{"assumption", "status", "verdict", "detail"}` — no
`observed` key.

### Freshness and reporting

| Method | Returns | Notes |
| --- | --- | --- |
| `sweep_stale()` | `list[str]` | Ids degraded to `STALE`. |
| `summary()` | `dict` | Calls `sweep_stale()` first. |
| `critical_blockers()` | `list[dict]` | `[{"assumption": {...}, "decisions": [...]}]`. |
| `live_assumptions()` | `list[Assumption]` | `VALID`, `STALE`, `UNVERIFIED`. |
| `render_reinjection(limit=25)` | `str` | `""` when nothing is live. |
| `render_status(limit=20)` | `str` | Never empty. |
| `render_assumption(asm_id)` | `str` | `"No assumption <id>"` when unknown. |

Real output of `critical_blockers()`:

```json
[
  {
    "assumption": {
      "id": "asm_2199bf66ac95",
      "key": "the-file-config-prod-json-exists",
      "statement": "the file config/prod.json exists",
      "check": {"expected_status": 200, "kind": "http_status", "url": "https://down.invalid/"},
      "status": "INVALIDATED",
      "provenance": {"extractor": "manual", "origin": "manual", "recorded_at": 1793998546.8551898,
                     "source_text": ""},
      "created_at": 1793998546.8551967,
      "updated_at": 1793998546.8704343,
      "last_checked_at": 1793998546.8704
    },
    "decisions": [ /* ... */ ]
  }
]
```

## Python API: LedgerStore

```python
from ledger import LedgerStore
store = LedgerStore("path/to/ledger.db")   # creates parent dirs and the schema
```

| Method | Returns | Notes |
| --- | --- | --- |
| `upsert_assumption(asm)` | `Assumption` | Inserts, or merges into the existing id: increments `seen_count`, accumulates provenance, preserves the first sighting and any existing invalidation. |
| `save_assumption(asm)` | `None` | Persists a mutated record (status, counters, timestamps). |
| `get_assumption(asm_id)` | `Assumption` or `None` | |
| `list_assumptions(status=None)` | `list[Assumption]` | Ordered by `created_at`. |
| `find_by_key(key)` | `Assumption` or `None` | Earliest match. |
| `add_evidence(assumption, verdict, detail, observed=None, source="", created_at=None)` | `Evidence` | |
| `evidence_for(asm_id, limit=50)` | `list[Evidence]` | Newest first. |
| `upsert_decision(dec)` | `Decision` | Merges `depends_on`; keeps a non-`open` state. |
| `save_decision(dec)` | `None` | |
| `get_decision(dec_id)` | `Decision` or `None` | |
| `list_decisions()` | `list[Decision]` | Ordered by `created_at`. |
| `decisions_depending_on(asm_id)` | `list[Decision]` | In-memory filter over `list_decisions()`. |
| `close()` | `None` | |

**Thread safety.** All operations take an `RLock`. The connection is opened with
`check_same_thread=False`, so a hook callback and a tool handler in the same process can both write.

**Provenance accumulation.** `_merge_provenance` keeps the first sighting under `first_origin` and
appends each distinct subsequent origin to `origins` exactly once. Re-observing the same assumption
therefore builds a history rather than overwriting it.

## Python API: verifiers

```python
from ledger.verifiers import run_check, CheckContext, SUPPORTED_KINDS
```

### `CheckContext`

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `root` | path-like | `None` | Base for relative paths. `None` means paths resolve as-is. |
| `cwd` | path-like | `os.getcwd()` | Working directory for git and command checks. |
| `http_get` | callable | `None` | Replaces the HTTP client. Called as `getter(url, timeout)`. |
| `runner` | callable | `subprocess.run` | Replaces the process runner. |
| `env` | dict | `os.environ` | Source for `env_set`. |
| `default_timeout` | float | `5.0` | Used when a spec omits `timeout`. |

`ctx.resolve(path)` resolves a relative path against `root`. `ctx.runner` is a property.

The injected `http_get` must return either `{"status": int, "body": str}` or
`{"error": str, "transient": True}`. It may also raise; that is treated as a provider failure.

### `run_check(spec, ctx=None)`

Dispatches a check spec to its verifier and returns a `CheckResult`. Anything unexpected — a
non-dict spec, an unknown `kind`, a verifier that raises, a wrong return type, an unknown verdict —
is normalised to `ERROR`. If the verifier returns a result with no `source`, it is set to
`f"verifier:{kind}"`.

`SUPPORTED_KINDS` is a sorted tuple of the twelve registered kinds.

## Python API: extractor

```python
from ledger.extractor import extract_from_text, extract_from_tool_result, Extracted
```

### `extract_from_text(text, *, source="message", max_items=50, allow_markers=True, allow_patterns=True)`

Returns `list[Extracted]`. `Extracted` has `statement`, `check`, `origin`
(`"marker"` or a pattern name), and `source_text` (the sentence it came from).

Deduplicates within the call by normalised statement, and truncates to `max_items`. Returns `[]`
for non-string or whitespace-only input. Never raises.

### `extract_from_tool_result(tool_name, result, *, max_items=20)`

Calls `extract_from_text` with `allow_patterns=False`, so only explicit markers are harvested.

Real outputs:

| Input | Extracted |
| --- | --- |
| `ASSUMPTION: the file config/prod.json exists` | `the file config/prod.json exists` → `{"kind": "file_exists", "path": "config/prod.json"}` |
| `ASSUME: HEAD is at commit 486f4a6a...` | `HEAD is at commit 486f4a6a...` → `{"kind": "git_commit", "commit": "486f4a6a..."}` |
| `PRECONDITION: on branch release/2.0` | `the repository is on branch release/2.0` → `{"kind": "git_branch", "branch": "release/2.0"}` |
| `- assuming: the endpoint https://api.test/health returns 200` | `the endpoint https://api.test/health returns HTTP 200` → `{"kind": "http_status", ...}` |
| `ASSUMPTION: everything is probably fine` | `everything is probably fine` → `{"kind": "unsupported"}` |
| `I think we are probably fine.` | *nothing* — no fabrication |
| `The table users has 3 rows.` | `the table users has exactly 3 rows` → `{"kind": "db_row_count", "table": "users", "equals": 3}` |
| `The table users has at least one row.` | `the table users has at least one row` → `{"kind": "db_row_count", "table": "users", "min": 1}` |

Recognised marker keywords: `assumption`, `assume`, `precondition`, `assuming`, each optionally
followed by `:` or `=`, optionally as a `-` or `*` list item, case-insensitive.

Patterns that produce a check: file exists, file contains, git branch, git commit (7–40 hex),
HTTP status, HTTP reachable, environment variable set, table row count. See
[CONFIGURATION.md](CONFIGURATION.md#check-specifications) for the check-spec fields each produces.

## Python API: model

```python
from ledger.model import (
    VALID, INVALIDATED, UNVERIFIED, STALE,      # lifecycle statuses
    PASS, FAIL, ERROR,                          # verifier verdicts
    Assumption, Evidence, Decision, CheckResult,
    status_after_check, assumption_id, decision_id, evidence_id,
    normalize_statement, slug, canonical_check_key,
)
```

### `status_after_check(previous_status, verdict, *, had_fresh_evidence)`

The single lifecycle rule. See
[VERIFICATION_AND_SAFETY.md](VERIFICATION_AND_SAFETY.md#the-critical-invariant).

### Records

| Dataclass | Fields |
| --- | --- |
| `CheckResult` | `verdict`, `detail`, `observed`, `source`; `to_dict()` |
| `Assumption` | `id`, `statement`, `check`, `status`, `key`, `provenance`, `created_at`, `updated_at`, `last_checked_at`, `invalidated_reason`, `invalidated_at`, `check_count`, `fail_count`, `seen_count`, `depends_on`; `to_dict()`, `is_fresh(freshness_seconds, now=None)` |
| `Evidence` | `id`, `assumption_id`, `verdict`, `detail`, `observed`, `source`, `created_at`; `to_dict()` |
| `Decision` | `id`, `statement`, `depends_on`, `state`, `key`, `created_at`, `blocked_reason`; `to_dict()` |

### ID helpers

| Function | Produces |
| --- | --- |
| `assumption_id(statement, check)` | `asm_<12 hex>` from normalised statement + canonical check key |
| `decision_id(statement)` | `dec_<12 hex>` from normalised statement |
| `evidence_id(assumption, verdict, created_at, source)` | `ev_<12 hex>` |
| `normalize_statement(text)` | Lowercased, whitespace-collapsed |
| `slug(text, limit=32)` | URL-safe slug, `"item"` when empty |
| `canonical_check_key(check)` | `kind` plus sorted JSON of the other fields |
