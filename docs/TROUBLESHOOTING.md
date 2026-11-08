# Troubleshooting

Diagnostic procedures for when the plugin is not behaving as expected.

- [First: get the real state](#first-get-the-real-state)
- [The plugin does nothing](#the-plugin-does-nothing)
- [The plugin warns or holds a turn unexpectedly](#the-plugin-warns-or-holds-a-turn-unexpectedly)
- [The plugin does not block when you expect it to](#the-plugin-does-not-block-when-you-expect-it-to)
- [Assumptions are not being captured](#assumptions-are-not-being-captured)
- [A check always returns UNVERIFIED](#a-check-always-returns-unverified)
- [A check always returns INVALIDATED](#a-check-always-returns-invalidated)
- [Everything is STALE](#everything-is-stale)
- [The slash command does not exist](#the-slash-command-does-not-exist)
- [The database is locked or corrupt](#the-database-is-locked-or-corrupt)
- [Storage is not where you expect](#storage-is-not-where-you-expect)
- [Diagnostic commands](#diagnostic-commands)
- [Escalation](#escalation)

## First: get the real state

Before changing anything, look at what the plugin actually believes. Most "bugs" are a correct
report of something you did not expect.

```
/assumptions
```

or, from the agent:

```
assumption_status({})
```

Then read the detail view for the specific record. That is where the evidence trail is:

```
/assumptions show <id>
```

The detail view shows the check spec, the counters, dependent decisions, and every evidence row
newest-first. In most cases the answer is already there.

## The plugin does nothing

No capture, no injection, no block. Work through these in order.

### 1. Is it enabled?

```bash
hermes plugins list
hermes plugins list --enabled
```

If it is not listed as enabled:

```bash
hermes plugins enable assumption-ledger
```

Hooks take effect in **new** sessions. If you enabled it while a session was running, start a new
one.

### 2. Does the doctor pass?

```bash
hermes plugins doctor <path-to-plugin>
```

Expected:

```
manifest: assumption-ledger 0.2.0 (standalone)
OK: runtime discovery, manifest parsing, import, and registration passed
registrations: 6 tool(s), 4 hook(s)
```

If the doctor reports a problem, it is almost always the manifest or the import — see the next two
sections.

### 3. Can the `ledger` package be imported?

The adapter imports `ledger` lazily, so an import failure degrades to "loads but registers
nothing". The symptom is exactly this: the plugin appears installed, and every tool returns an
error.

Test it directly:

```bash
cd <path-to-plugin>
python -c "import ledger; print(ledger.__version__)"
```

Expected: `0.2.0`.

If that fails, the plugin directory is incomplete or is not on the path the way Hermes loads it. A
tool call will show the reason:

```json
{"error": "ledger package unavailable: No module named 'ledger'"}
```

That string is the plugin telling you the cause. Fix the checkout, not the ledger.

### 4. Is the data directory writable?

If `LedgerHost.engine()` cannot build the engine, `LedgerHost.error` is set and every hook and tool
no-ops. Check:

```bash
ls -ld "<hermes home>/plugin-data/assumption-ledger/"
```

If the directory does not exist, it is created on first use. If it exists but is not writable by
the Hermes process, that is the cause. Fix permissions:

```bash
chmod u+rwx "<hermes home>/plugin-data/assumption-ledger/"
```

### 5. Are there any assumptions to act on?

A fresh install is completely inert: no records means `render_reinjection()` returns `""`,
`on_pre_llm_call` returns `None`, and nothing is blocked. Confirm with `/assumptions` —
`tracked: 0` means the plugin is working correctly and simply has nothing to report.

## The plugin warns or holds a turn unexpectedly

The message names the assumption and the decision. It is not a generic error:

```
Assumption Ledger warning: work recorded in this session depends on an assumption that evidence has contradicted.
- INVALIDATED: the file missing.txt exists (id asm_4097c971c7d0)
  relied on by: consume missing.txt
This is a warning, not a stop. To clear it: /assumptions show <id>, /assumptions resolve <id> (re-check), or /assumptions drop <id> (retire).
```

A warning is delivered as context and the turn proceeds. The turn is only *held* if it reaches the
edited-code verify gate with the block still standing. Both are the plugin working as designed.

Work out whether the contradiction is real:

```
/assumptions show <the-assumption-id>
```

Read the evidence trail. If the last verdict is `fail`, a deterministic check observed something
that contradicts the assumption. The legitimate responses are in
[USER_GUIDE.md](USER_GUIDE.md#when-the-plugin-warns-you): fix reality and re-check, retire the
assumption, or change the plan.

Note the session scope: a block recorded in one session does not warn another. If you expected a
warning and saw none, confirm you are in the session that recorded the dependent decision.

### If the contradiction is stale or was a mistake

The record is `INVALIDATED` and stays that way until **refuting evidence** arrives — a passing
check. An unreachable provider will not clear it; that is the invariant working, not a bug. If the
underlying condition genuinely is fine now, re-check:

```
/assumptions check <id>
```

A `pass` sets it back to `VALID` and unblocks the decision.

### If the block is genuinely wrong

Two possibilities, and they are different problems:

1. **The check spec is wrong.** The assumption is being tested incorrectly — for example an
   `http_status` expecting 200 against an endpoint that legitimately returns 204. Fix it by
   recording a corrected assumption with the right spec, then update the decision's dependencies.
   You cannot edit a spec in place; the id is derived from the statement plus the check.
2. **The dependency should not exist.** The decision does not really rely on that assumption.
   Record a new decision without it.

Do **not** delete the database to clear a block. That discards the evidence you need to understand
the problem.

## The plugin does not block when you expect it to

The gate has one condition: an `INVALIDATED` assumption with **at least one dependent decision**.
Check each part.

### Is the assumption actually INVALIDATED?

```
/assumptions
```

Look at the `invalidated=` count and the `INVALIDATED (contradicted by evidence)` section. If the
record is `STALE`, not `INVALIDATED`, the check could not be run — a timeout or a transient status.
`STALE` never blocks.

The most common surprise: **a `500` does not invalidate.** Statuses 408, 425, 429, 500, 502, 503
and 504 are treated as transient and produce `error` → `STALE`. To assert that an endpoint is
broken, record `expected_status: 500` — an explicitly expected status is a legitimate `pass`. A
`404` is definitive, so it does invalidate.

### Does a decision depend on it?

```
/assumptions show <id>
```

Look for a `dependent decisions:` section. If it is absent, nothing depends on the assumption, so
there is nothing to block. This is intentional — an invalidated assumption nobody relies on must
not halt unrelated work.

To create the dependency:

```
assumption_decide({"statement": "your plan step", "depends_on": ["<the-assumption-id>"]})
```

### Was the decision recorded before the invalidation?

Propagation runs on every verification, so order does not matter: `_recompute_decision_block` is
called when a decision is recorded **and** on every check outcome. A decision recorded after an
invalidation is computed as `blocked` immediately.

### Is the turn actually running?

`pre_llm_call` fires once per turn before inference. If the session is doing something that does
not go through that path, the gate does not run. `pre_verify` only fires when the agent edited
code.

## Assumptions are not being captured

### From your own replies

Check whether the text matches a rule. Capture needs either an explicit marker or a
natural-language pattern. See
[USER_GUIDE.md](USER_GUIDE.md#writing-assumptions-the-extractor-understands).

`I think we are probably fine.` extracts nothing, by design. Write:

```
ASSUMPTION: the file config/prod.json exists
```

### From tool output

**Tool output only yields explicit markers.** Natural-language patterns are deliberately not
applied there, so that a directory listing containing `file.txt exists` does not manufacture an
assumption. Running `git rev-parse HEAD` creates nothing; writing
`ASSUMPTION: HEAD is at commit <sha>` does.

### The marker was there but nothing was recorded

Check the record count before and after with `/assumptions`. If the count did not change, the
statement may have **merged** into an existing record rather than creating a new one. Records are
keyed by normalised statement plus check, so `The file dup.txt exists` and `the file dup.txt exists`
are one record, and the second observation increments `seen_count`. Verify with
`/assumptions show <id>` — a `seen:` count above 1 means it merged.

## A check always returns UNVERIFIED

`UNVERIFIED` means no verdict could be reached. The `detail` field says why. Common causes:

| Detail contains | Cause | Fix |
| --- | --- | --- |
| `unsupported check kind` | The `kind` is not one of the twelve. | Use a supported kind. |
| `no path supplied` / `path and sha256 are both required` | A required field is missing. | Add it to the check spec. |
| `provider unavailable` | DNS or connection failure. | Check the URL and network. |
| `request failed` / `timed out` | Timeout or transport error. | Raise `timeout`, or check the endpoint. |
| `transient HTTP 500` (or 429, 502, …) | A transient status. | Expected. Use `expected_status` to assert it deliberately. |
| `no deterministic check available` | The check kind is `unsupported`. | The assumption has no testable form. Record one that does. |
| `git executable not found` | `git` is not installed or not on `PATH`. | Install git. |
| `git command timed out` | Git exceeded the timeout. | Raise `timeout` in the spec. |
| `git could not run` | An `OSError` launching git — usually a bad directory. | Check the resolved `cwd`. |
| `git exited <n>` / a git error message | Git ran and failed — commonly not a repository. | Check that the resolved path is a repository. |
| `file not present, cannot hash` | The file to hash is missing. | Check the resolved path. |
| `cannot hash` | An unknown algorithm name, or the file could not be read. | Use a valid `hashlib` name. |
| `unsafe table name` | A `db_row_count` table name failed validation. | Use `[A-Za-z_][A-Za-z0-9_]*`. |
| `path and table are both required` | A `db_*` spec is missing a field. | Add `path` and `table`. |
| `database file not present` | The SQLite file does not exist. | Check the resolved path. |
| `cannot open database read-only` | The file is not a readable database. | Check the path and permissions. |
| `query failed` | The SQL was rejected or the table is gone. | Check the `table` and `where` values. |

Read the exact detail string with `/assumptions show <id>` — the evidence row has the full text.

### "unsupported" is permanent

A record whose check is `{"kind": "unsupported"}` can never become `VALID`. That is what the
extractor assigns to a marker with no checkable form, such as
`ASSUMPTION: everything is probably fine`. The record is kept so the stated belief is not lost, and
it will stay `UNVERIFIED` forever. If you want it verified, record a new assumption with a real
check spec.

## A check always returns INVALIDATED

`INVALIDATED` means a check **definitively** contradicted the assumption. Look at the reason:

| Reason | Meaning |
| --- | --- |
| `path does not exist` | The file is not there. |
| `digest differs` | The content changed. |
| `different branch checked out` | Not on the expected branch. |
| `HEAD differs` | The commit does not match. |
| `HTTP 404 not in [200]` | A definitive non-transient status mismatch. |
| `table missing` | The table is not in the database. |
| `row count differs` | Outside the expected bound. |

The usual cause is a **path resolved differently than you assumed**. Relative paths resolve against
`CheckContext.root`, which under Hermes is the agent's working directory. Use `/assumptions show
<id>` to see the resolved path in the evidence:

```
check: {'kind': 'file_exists', 'path': 'config/prod.json'}
evidence (newest first):
  - fail: path does not exist
```

If the resolved path is wrong, record a corrected assumption with an explicit path.

### A note on git checks and `cwd`

Git and command checks resolve their directory from `spec.get("cwd", ".")` **against
`CheckContext.root`**. The context's own `cwd` field is stored but never read — setting
`CheckContext(cwd=...)` has no effect on any check. If you need a git check against a specific
directory, set `cwd` **in the check spec**:

```json
{"kind": "git_branch", "branch": "main", "cwd": "/path/to/repo"}
```

This is a real implementation detail, verified by testing that a nonexistent `ctx.cwd` produces
identical results. See [CONFIGURATION.md](CONFIGURATION.md#git) for the check fields.

## Everything is STALE

`STALE` means "was valid, needs re-checking". Two causes, and they look the same in the status view
but differ in the evidence trail.

### Aged out

The evidence is older than `freshness_seconds` (86400, i.e. one day, and not configurable — see
[CONFIGURATION.md](CONFIGURATION.md#declared-configuration-options-are-not-currently-read-at-runtime)).
`sweep_stale()` degrades it, which happens when `summary()` or `on_pre_verify` runs. The evidence
row explains the window.

**Fix:** re-check. `/assumptions check <id>`, or `/assumptions check` for everything.

### Could not refresh

The assumption was `VALID`, and a re-check produced `error` — a timeout, a transient status, an
unreachable provider. The status degrades to `STALE` rather than staying `VALID`, because the
evidence can no longer be confirmed.

**Fix:** re-check once the provider is reachable. This resolves on its own; it is not a defect.

Note that this is **not** what happens to an `INVALIDATED` record. A transient error leaves a
contradicted assumption `INVALIDATED` and keeps its dependent decisions blocked. That asymmetry is
the core invariant, and it is covered by
[VERIFICATION_AND_SAFETY.md](VERIFICATION_AND_SAFETY.md#the-critical-invariant).

## The slash command does not exist

If `/assumptions` is not recognised, the command did not register. Two likely causes:

1. **The plugin is not enabled or not loaded.** See
   [The plugin does nothing](#the-plugin-does-nothing).
2. **A name conflict.** Hermes refuses a plugin command that collides with a built-in command,
   logging `tried to register command '/assumptions' which conflicts with a built-in command`. The
   plugin logs this and continues; the other registrations still work. Check the Hermes log.

The tools (`assumption_status` and friends) are registered independently of the slash command, so
if the tools work and only the command is missing, a conflict is the likely cause.

## The database is locked or corrupt

### Locked

`database is locked` means another connection holds a write lock. The store opens in WAL mode
specifically so a reader and a writer can coexist, so this usually means:

- Two Hermes processes are using the same data directory. WAL allows concurrency, not two writers
  in separate processes indefinitely.
- The data directory is on a network filesystem where WAL is unsupported. The store falls back to
  `DELETE` journal mode, which serialises readers and writers and makes this much more likely.
- A stale process still holds the connection.

**Fix:** stop the extra process, or move the data directory to a local filesystem. WAL requires
shared memory; a network mount may not provide it.

### Corrupt

If the engine fails to construct, `LedgerHost` catches it and the plugin no-ops — it will not take
the agent down. To inspect the file:

```bash
sqlite3 "<data dir>/ledger.db" "PRAGMA integrity_check;"
```

`ok` means it is fine and the problem is elsewhere. Anything else means the file is damaged. There
is no repair tool in the plugin and no migration system; a damaged database must be replaced. That
loses all records, so salvage first if the data matters:

```bash
sqlite3 "<data dir>/ledger.db" ".dump" > backup.sql
```

Only after salvaging should you move the file aside and let the plugin recreate it. **This is the
last resort, not the first step** — the database is usually the evidence that explains the problem.

## Storage is not where you expect

`_data_dir()` resolves in this order:

| Order | Condition | Path |
| --- | --- | --- |
| 1 | Hermes runtime importable | `<hermes home>/plugin-data/assumption-ledger/` |
| 2 | `HERMES_HOME` set | `$HERMES_HOME/plugin-data/assumption-ledger/` |
| 3 | Neither | `~/.hermes/plugin-data/assumption-ledger/` |

The most common surprise is **`HERMES_HOME` being set to something unexpected**, which redirects
storage away from the default profile. Check it:

```bash
echo "$HERMES_HOME"
```

If it is set and you did not intend it, either unset it or look under that path. Note that Hermes
profiles each have their own home, so records are per-profile: switching profiles switches
ledgers, and records do not carry across.

If records seem to have vanished, check whether the profile changed before assuming data loss.
Records survive `hermes plugins update` and `hermes plugins remove`, because they live under
`plugin-data/` rather than the install directory.

## Diagnostic commands

```bash
# What is installed and enabled?
hermes plugins list
hermes plugins list --enabled

# Is the plugin structurally valid? (15 checks, expect all pass)
hermes plugins validate <path-to-plugin>

# Does it load and register? (expect 4 tools, 4 hooks)
hermes plugins doctor <path-to-plugin>

# What capabilities were declared vs granted?
hermes plugins capabilities assumption-ledger

# Can the engine be imported standalone?
cd <path-to-plugin> && python -c "import ledger; print(ledger.__version__)"

# Where is the data?
echo "$HERMES_HOME"
ls -la "<hermes home>/plugin-data/assumption-ledger/"

# Is the database intact?
sqlite3 "<data dir>/ledger.db" "PRAGMA integrity_check;"

# What is in it?
sqlite3 "<data dir>/ledger.db" "SELECT status, COUNT(*) FROM assumptions GROUP BY status;"
sqlite3 "<data dir>/ledger.db" "SELECT verdict, detail FROM evidence ORDER BY created_at DESC LIMIT 20;"
sqlite3 "<data dir>/ledger.db" "SELECT state, statement FROM decisions;"

# Do the tests pass? (expect 246 passed)
cd <path-to-plugin> && python -m pytest -q
```

Inside a session:

```
/assumptions                    # status overview
/assumptions list               # one line per record
/assumptions show <id>          # full detail with evidence trail
/assumptions check              # verify everything, then show status
```

## Escalation

If the diagnostics above do not explain the behaviour, gather this before reporting:

1. `hermes plugins doctor <path>` output.
2. `hermes plugins validate <path>` output.
3. `python -m pytest -q` result.
4. The `/assumptions` output.
5. The `/assumptions show <id>` output for the record in question — this is the most useful item,
   because it contains the check spec, the resolved values, and the evidence trail.
6. The plugin version (`0.2.0`) and the Hermes version.

**Redact before posting.** The detail view can contain filesystem paths, and provenance can contain
up to 500 characters of the sentence an assumption came from. Remove anything sensitive. Never
attach `ledger.db` — it contains every assumption and its evidence.

Report issues at
`https://github.com/itzebe/hermes-assumption-ledger/issues`. For security issues, use the private
advisory process in [SECURITY.md](SECURITY.md#reporting-a-vulnerability) instead.
