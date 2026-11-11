# User Guide

How to use Assumption Ledger day to day. This guide assumes it is already installed — see
[INSTALLATION.md](INSTALLATION.md) if not.

- [The problem it solves](#the-problem-it-solves)
- [The four things you do](#the-four-things-you-do)
- [How capture happens automatically](#how-capture-happens-automatically)
- [Writing assumptions the extractor understands](#writing-assumptions-the-extractor-understands)
- [A complete worked example](#a-complete-worked-example)
- [Reading the status view](#reading-the-status-view)
- [Understanding the four statuses](#understanding-the-four-statuses)
- [When the plugin blocks a turn](#when-the-plugin-blocks-a-turn)
- [Everyday recipes](#everyday-recipes)
- [What to expect from re-injection](#what-to-expect-from-re-injection)
- [Common mistakes](#common-mistakes)

## The problem it solves

An agent makes a claim, acts on it, and later the claim stops being true. Nothing carries that
fact forward. The agent does not re-check, because it does not remember it claimed anything.

Assumption Ledger records the claim and the way to test it, tests it deterministically, and — when
the test contradicts the claim — holds the work that depends on it.

The distinguishing behaviour is what happens when verification *fails to run*. A timeout, a rate
limit, an unreachable provider: none of these may be read as "the claim is false". Uncertainty is
recorded as uncertainty, and a claim already known to be contradicted stays contradicted until
real evidence clears it. That rule is documented in full in
[VERIFICATION_AND_SAFETY.md](VERIFICATION_AND_SAFETY.md#the-critical-invariant).

## The four things you do

| You want to | Use |
| --- | --- |
| Record something you are relying on | tool `assumption_record`, or write `ASSUMPTION: ...` in your reply |
| Re-test it | tool `assumption_check`, or `/assumptions check [id]` |
| Declare what depends on it | tool `assumption_decide` |
| See where things stand | tool `assumption_status`, or `/assumptions` |

Capture also happens on its own from your messages and from tool output. You rarely need to call
`assumption_record` explicitly if you write assumptions in a form the extractor recognises.

## How capture happens automatically

Two hooks run without you doing anything:

| Hook | Fires | What it captures |
| --- | --- | --- |
| `post_llm_call` | after your reply | Explicit markers **and** natural-language patterns |
| `post_tool_call` | after every tool call | **Nothing.** Tool output is never captured. |

The asymmetry is deliberate, and it is about trust. Your own reply is your statement. Tool output
is text you merely read: a web page, a file or a terminal listing can contain the literal string
`Assumption: ...`, and storing it would plant a durable record that is re-injected into every later
turn.

A consequence worth knowing: **if you want a tool observation tracked, state it in your reply.**
Running `git rev-parse HEAD` does not create a `git_commit` assumption. Writing `ASSUMPTION: HEAD
is at commit <sha>` does, and so does an explicit `assumption_record` call.

## Writing assumptions the extractor understands

### Explicit markers (highest confidence)

Any line containing one of these keywords, optionally followed by `:` or `=`, optionally as a `-`
or `*` list item, case-insensitive:

```
assumption    assume    precondition    assuming
```

Examples that work:

```
ASSUMPTION: the file config/prod.json exists
ASSUME: HEAD is at commit 486f4a6a9918a4054373e33a200260ffd501645e
PRECONDITION: on branch release/2.0
- assuming: the endpoint https://api.test/health returns 200
```

A marker with no checkable form is still captured, with the check `{"kind": "unsupported"}`:

```
ASSUMPTION: everything is probably fine
  -> stored, status UNVERIFIED, never verifiable
```

That is intentional. The alternative is discarding a belief you stated.

### Natural-language patterns (your reply only)

If a sentence matches a pattern, a check is inferred:

| You write | Check inferred |
| --- | --- |
| `the file config/prod.json exists` | `file_exists` on `config/prod.json` |
| `the file config/prod.json contains "region"` | `file_contains` with that pattern |
| `we are on branch release/2.0` | `git_branch` = `release/2.0` |
| `HEAD is at commit 486f4a6a...` | `git_commit` (7–40 hex characters) |
| `https://api.example.com/health returns 200` | `http_status` 200 |
| `https://api.example.com is reachable` | `http_status` 200 |
| `the environment variable DATABASE_URL is set` | `env_set` |
| `the table users has 3 rows` | `db_row_count` = 3 |
| `the table users has at least one row` | `db_row_count` min 1 |

### What produces nothing

Prose without a marker and without a matching pattern produces no assumption:

| Text | Extracted |
| --- | --- |
| `I think we are probably fine.` | nothing |
| `Let's deploy when ready.` | nothing |
| `The build finished successfully.` | nothing |

This is the conservative-by-design property. Missing an assumption is treated as less harmful than
inventing one.

### Two statements, one record

Records are keyed by the normalised statement **plus** the check. Case and whitespace are
normalised, so:

```
the file dup.txt exists
The file dup.txt exists
```

are one record, and the second observation increments `seen_count` rather than creating a
duplicate. But the same statement with a *different* check is a **separate** record — the id
identifies an assertion-and-how-to-test-it pair.

## A complete worked example

This is real output from the plugin, run against a real git repository and a real local HTTP
endpoint. Only the paths and SHAs are abbreviated.

### Step 1: state what you are relying on

You reply:

```
ASSUME: HEAD is at commit 68a1767e55326c0506be0edcaa3fa628514e4de4
ASSUMPTION: the endpoint http://127.0.0.1:48975/health returns 200
```

The ledger captures both:

```
captured asm_86ef5f6c9e29  HEAD is at commit 68a1767e55326c0506be0edcaa3fa628514e4de4
captured asm_887eaef163ac  the endpoint http://127.0.0.1:48975/health returns HTTP 200
```

### Step 2: declare what depends on them

```
assumption_decide({"statement": "promote release 2.0",
                   "depends_on": ["asm_86ef5f6c9e29", "asm_887eaef163ac"]})

{"id": "dec_f6f98559f4d5", "state": "open", "blocked_reason": null}
```

The decision is `open` because neither assumption is invalidated yet. Note that being
`UNVERIFIED` does **not** block anything — an untested assumption is a reason to test, not a
reason to halt.

### Step 3: verify

```
assumption_check({})

  VALID       pass  HEAD is at commit 68a1767e55326c0506be0edcaa3fa628514e4de4
  VALID       pass  the endpoint http://127.0.0.1:48975/health returns HTTP 200
```

Both pass. The status view:

```
Assumption Ledger
  tracked: 2  valid=2  invalidated=0  unverified=0  stale=0
  live assumptions (in effect):
    - [VALID] asm_86ef5f6c9e29 HEAD is at commit 68a1767e55326c0506be0edcaa3fa628514e4de4
    - [VALID] asm_887eaef163ac the endpoint http://127.0.0.1:48975/health returns HTTP 200
```

### Step 4: reality changes

The endpoint now returns `404` — a **definitive** answer, not a transient one. You re-check:

```
  VALID       pass  HEAD is at commit 68a1767e55326c0506be0edcaa3fa628514e4de4
  INVALIDATED fail  the endpoint http://127.0.0.1:48975/health returns HTTP 200
```

The decision is blocked automatically:

```
decision state=blocked
  depends on invalidated assumption(s): asm_887eaef163ac
    (the endpoint http://127.0.0.1:48975/health returns HTTP 200)
```

### Step 5: the provider goes down

You re-check while the endpoint is unreachable. This is the important step:

```
  VALID       pass  HEAD is at commit 68a1767e55326c0506be0edcaa3fa628514e4de4
  INVALIDATED error the endpoint http://127.0.0.1:48975/health returns HTTP 200
  -> decision state=blocked
```

Read the verdict column: `error`, not `fail`. The plugin could not reach the endpoint. And yet the
status is still `INVALIDATED` and the decision is still `blocked`. **The outage did not clear the
contradiction.** That is the whole point of the plugin.

### Step 6: recovery

The endpoint returns 200. You re-check:

```
  VALID       pass  HEAD is at commit 68a1767e55326c0506be0edcaa3fa628514e4de4
  VALID       pass  the endpoint http://127.0.0.1:48975/health returns HTTP 200
  -> decision state=open  blocked_reason=None
```

Refuting evidence cleared the invalidation and unblocked the decision. Note that blocking is
**reversible** — a repaired assumption releases the work it was holding.

## Reading the status view

```
Assumption Ledger
  tracked: 3  valid=2  invalidated=1  unverified=0  stale=0
  live assumptions (in effect):
    - [VALID] asm_8f39aa2c3275 HEAD is at commit 5ee308fe7294d82a294f2a017fb61bc296caf3d0
    - [VALID] asm_89c51e608b6a the repository is on branch master
  INVALIDATED (contradicted by evidence):
    - asm_2199bf66ac95 the file config/prod.json exists — path does not exist
  BLOCKED decisions:
    - dec_6a4287df0167 deploy release 2.0 to production
```

| Section | Meaning |
| --- | --- |
| `tracked` | Total records, including invalidated and stale. |
| `valid=` / `invalidated=` / `unverified=` / `stale=` | Counts per status. |
| `live assumptions` | `VALID`, `STALE` and `UNVERIFIED` records — what is currently in effect. |
| `INVALIDATED` | Contradicted records, each with the reason. |
| `BLOCKED decisions` | Decisions with at least one invalidated dependency. |

A detail view, from `/assumptions show <id>`:

```
asm_2199bf66ac95  [VALID]
  statement: the file config/prod.json exists
  check: {'kind': 'file_exists', 'path': 'config/prod.json'}
  provenance: assistant (extractor=marker)
  created: 2026-11-06 22:34:19
  checks: 4  fails: 2  seen: 1
  dependent decisions:
    - dec_6a4287df0167 [open] deploy release 2.0 to production
  evidence (newest first):
    - pass: path exists
    - fail: path does not exist
    - fail: path does not exist
    - pass: path exists
```

The evidence list is the audit trail, newest first. In that example you can read the whole history:
it passed, then failed twice (the config was deleted), then passed again (it came back). The status
column shows `VALID` because the most recent verdict was a pass. Note `checks: 4  fails: 2` —
counters accumulate across the record's life and are never reset.

## Understanding the four statuses

| Status | Meaning | Does it block work? |
| --- | --- | --- |
| `UNVERIFIED` | Recorded, not yet tested — or tested but no verdict could be reached. | No |
| `VALID` | The check passed and the evidence is fresh. | No |
| `STALE` | Was valid; either the evidence aged out or it could not be refreshed. | No |
| `INVALIDATED` | Evidence definitively contradicts it. | **Yes**, for decisions that depend on it |

Two things surprise people:

**`UNVERIFIED` never blocks.** A brand-new assumption is `UNVERIFIED`. If that blocked work, every
recorded assumption would halt the agent immediately. Untested is not the same as contradicted.

**`STALE` never blocks.** Stale means "re-check before relying on this", not "this is false".

Only `INVALIDATED` blocks, and only the decisions that named it in `depends_on`.

## When the plugin warns you

The warning is conditional by construction: it appears only when an `INVALIDATED` assumption has at
least one dependent decision **recorded in the same session**. When it fires, you see:

```
Assumption Ledger warning: work recorded in this session depends on an assumption that evidence has contradicted.
- INVALIDATED: the file missing.txt exists (id asm_4097c971c7d0)
  relied on by: consume missing.txt
This is a warning, not a stop. To clear it: /assumptions show <id>, /assumptions resolve <id> (re-check), or /assumptions drop <id> (retire).
```

The turn is not stopped. You keep the turn you need to investigate. If it reaches the edited-code
verify gate with the block still standing, `pre_verify` holds it there with the same instructions.

Three ways forward:

1. **Fix reality, then re-check** — `/assumptions resolve <id>`. If the evidence now passes, the
   assumption becomes `VALID` and the decision unblocks. `resolve` never forces a status: a still
   failing check keeps the block.
2. **Retire it** — `/assumptions drop <id>`. This removes it from the live set and unblocks its
   decisions, and records that a user retired it without verification. Use this when the assumption
   was simply wrong to track; the ledger will not claim it was ever proven.
3. **Change the plan.** The dependency is what creates the warning. If the assumption is no longer
   load-bearing, record a new decision without it, or repair the old one with
   `link_decision` semantics.

What you should **not** do is treat the warning as a bug to route around. It is reporting a real
contradiction between what you claimed and what a deterministic check observed.

An invalidated assumption that **nothing depends on** produces no warning at all. The gate is
dependency-aware and session-scoped, not global.

### Blocks do not cross sessions

A decision recorded in session A does not warn session B, even when both run at once. This is
deliberate: a contradiction raised in a long-running session must not halt every other session, and
a background session must not silently stop your foreground work. When you resume the affected
session, the warning is still there — it is read from durable storage, not memory.

## Everyday recipes

### Track a deploy's release identity

```
ASSUME: HEAD is at commit <sha>
ASSUMPTION: the endpoint https://your-app.example.com/health returns 200
ASSUMPTION: the release identity at /health is release 2.0
```

Then record the decision and check:

```
assumption_decide({"statement": "promote release 2.0 to production", "depends_on": [...]})
assumption_check({})
```

For the third one to be verifiable, give it a real check — an `http_json_field` against a version
endpoint:

```
assumption_record({
  "statement": "the deployed release identity is 2.0",
  "check": {"kind": "http_json_field", "url": "https://your-app.example.com/version",
            "pointer": "release", "equals": "2.0"}})
```

### Track a config file your build depends on

```
ASSUMPTION: the file config/prod.json exists
ASSUMPTION: the file config/prod.json contains "region"
```

### Track that the tree you are committing is clean

```
ASSUMPTION: the working tree is clean
```

Note that this ignores untracked files by default. See
[CONFIGURATION.md](CONFIGURATION.md#git) if you want them counted.

### Track a database migration

```
assumption_record({"statement": "the users table exists",
                   "check": {"kind": "db_table_exists", "path": "app.db", "table": "users"}})
assumption_record({"statement": "the users table has at least one row",
                   "check": {"kind": "db_row_count", "path": "app.db", "table": "users", "min": 1}})
```

Both open the database read-only.

### Track that an environment variable is set

```
ASSUMPTION: the environment variable DATABASE_URL is set
```

Presence only. The value is never recorded, because it may be a secret.

## What to expect from re-injection

Once per turn, before the model is called, the live assumptions are injected into the **user
message** — never the system prompt:

```
Assumption Ledger — operational assumptions currently in effect:
  [VALID] the file app.py exists  (id asm_4097c971c7d0)
  [UNVERIFIED] the file config/prod.json exists  (id asm_2199bf66ac95)
```

Practical points:

- **It is capped.** `render_reinjection(limit=25)` shows at most 25 records. The plugin's own call
  uses that default, so with more than 25 live assumptions you will see a subset.
- **Invalidated records are not in this block.** They appear only in the blocker message, which is
  mutually exclusive with the context block.
- **When a blocker exists you get the block, not the context.** The turn ends before inference, so
  no injection happens.
- **With nothing live, the hook returns `None`.** A fresh install is completely inert.

## Common mistakes

**Expecting tool output to be tracked automatically.** It is not, unless you marked it. Write an
explicit `ASSUMPTION:` line.

**Expecting `UNVERIFIED` to block.** It does not. If you want a gate, record a decision that
depends on the assumption.

**Forgetting `depends_on`.** An invalidated assumption with no dependents blocks nothing. The gate
is created by the dependency, not by the invalidation.

**Expecting an outage to invalidate.** It never does. An unreachable provider produces `error`,
which is uncertainty. A previously-valid assumption degrades to `STALE`; a previously-invalidated
one stays `INVALIDATED`.

**Expecting a `500` to invalidate.** `500`, `502`, `503`, `504`, `429`, `408` and `425` are treated
as transient, so they produce `error` → `STALE`, not `INVALIDATED`. To assert that an endpoint is
broken, record `expected_status: 500` — an explicitly expected status is a legitimate pass. A `404`
is definitive, so it *does* invalidate.

**Trying to edit an assumption's statement.** You cannot. The id is derived from the statement and
check, so a corrected statement is a new record with a new id. Update the decision's dependencies
instead.

**Assuming the plugin scans for secrets.** It does not. See
[SECURITY.md](SECURITY.md#sensitive-information-handling).

**Trying to tune `freshness_seconds`.** It is read at runtime and configurable. See
[CONFIGURATION.md](CONFIGURATION.md#declared-configuration-options).

**Deleting the ledger to fix a problem.** The database is usually the evidence you need, not the
cause. See [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
