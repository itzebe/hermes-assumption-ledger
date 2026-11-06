# Security

This document describes what Assumption Ledger trusts, what it can access, what it stores, and
where its boundaries are. It distinguishes **verified properties** (observed in the source or by
running the code) from **design intentions** (what the design is trying to achieve).

- [Threat model](#threat-model)
- [Security boundaries](#security-boundaries)
- [Trust assumptions](#trust-assumptions)
- [Audit status](#audit-status)
- [Local storage](#local-storage)
- [Data retention](#data-retention)
- [Sensitive information handling](#sensitive-information-handling)
- [Network access](#network-access)
- [External LLM calls](#external-llm-calls)
- [Credentials](#credentials)
- [Permission boundaries](#permission-boundaries)
- [Known security limitations](#known-security-limitations)
- [Failure modes](#failure-modes)
- [Reporting a vulnerability](#reporting-a-vulnerability)

## Threat model

### What this plugin defends against

The **accidental** failure mode: an agent acts on a belief that has been contradicted by evidence,
because nothing carried that fact forward. Concretely:

- The agent assumes a config file exists; the file is deleted; the agent deploys anyway.
- The agent assumes it is on a release branch; a rebase moved it; the agent pushes.
- The agent assumes a health endpoint returns 200; it now returns 500; the agent calls the release
  good.

The plugin's defence is to record the belief, test it deterministically, and hold the *dependent*
work when the test contradicts it.

### What this plugin does not defend against

- **A malicious agent.** An agent that wants to bypass the ledger can simply not record a
  dependency, or write an assumption whose check is trivially true. The ledger records what the
  agent chooses to state. It is a discipline aid, not a sandbox.
- **A malicious plugin or a compromised Hermes host.** This plugin runs in the same process as
  everything else. It offers no isolation.
- **A malicious check spec.** Specs come from the agent or the operator. See
  [SQL interpolation in db_row_count](#sql-interpolation-in-db_row_count) and
  [`command_succeeds` runs an arbitrary argv](#command_succeeds-runs-an-arbitrary-argv).
- **Supply-chain compromise of dependencies.** The plugin has **no third-party runtime
  dependencies** (`pyproject.toml` declares `dependencies = []`), which removes most of this
  surface. It uses only the standard library.
- **Anything outside the four lifecycle states.** See
  [VERIFICATION_AND_SAFETY.md](VERIFICATION_AND_SAFETY.md#what-this-does-not-guarantee).

## Security boundaries

| Boundary | Property |
| --- | --- |
| Process | Runs in-process with Hermes. No separate service, no daemon, no open port. |
| Network | Makes **no** request of its own. The only outbound traffic is the HTTP checks you explicitly record. |
| Credentials | Reads **no** credential. `requires_env: []`. |
| Filesystem | Reads the paths named in check specs. Writes only inside its own data directory. |
| Database | Opens SQLite files read-only (`mode=ro`). |
| Subprocesses | Runs `git` for git checks and the `argv` you specify for `command_succeeds`. |
| Model | Makes no LLM call. |

## Trust assumptions

The plugin trusts:

1. **The Hermes host.** `register(ctx)`, the hook payloads, and `plugins.plugin_storage` are
   assumed to be genuine. A hostile host can do anything.
2. **The agent's stated assumptions.** They are recorded as stated. The plugin does not try to
   detect a lie.
3. **The filesystem, git and the network as evidence sources.** A `pass` means the check observed
   the expected evidence. It does not mean the evidence source is honest.
4. **The operator's check specs.** `where` clauses and `argv` are executed as given. This is a
   deliberate capability, not an oversight, and it is documented below.

The plugin does **not** trust: any model output (it is parsed, never executed), any string that
reaches SQL without validation (table names are validated), or the assumption that a previous
`pass` still holds (hence freshness).

## Audit status

**This plugin has not been security-audited, and no certification or attestation is claimed.**
There has been no third-party review, no penetration test, and no formal verification. Everything
in this document is the result of reading the source and running the code. Statements marked
"verified" were observed directly; everything else is a design intention.

The upstream plugin validator performs an automated **security scan** as part of its checks, and
this plugin passes it (`security scan: safe`, `no core override: no runtime rebinds of Hermes
core`). That is a linter result, not an audit.

## Local storage

All state is local. Three tables in one SQLite file under the plugin data directory:

```
<hermes home>/plugin-data/assumption-ledger/ledger.db
```

Nothing is sent anywhere. There is no telemetry, no analytics, no phone-home, and no sync.

**Verified:** the connection is opened with `PRAGMA journal_mode=WAL` (falling back to `DELETE`
when the filesystem cannot support WAL), `PRAGMA foreign_keys=ON`, and
`check_same_thread=False` guarded by a per-store `RLock`.

### WAL behaviour

Write-ahead logging means recent writes live in `ledger.db-wal` until a checkpoint, and
`ledger.db-shm` holds the shared-memory index. Two consequences:

- **Backups must include the `-wal` and `-shm` files**, or use SQLite's backup API. Copying
  `ledger.db` alone can lose recent evidence.
- **WAL files may contain evidence you consider sensitive**, because evidence details and observed
  values are written there. Treat the whole directory as data, not just the `.db`.

The fallback to `DELETE` journal mode on unsupported filesystems (for example some network mounts)
is a correctness fallback: the plugin keeps working, without WAL's concurrency benefits.

### Data retained

Per assumption: the statement, the check spec, provenance (origin, extractor name, up to 500
characters of the source sentence, timestamps, accumulated origins), status, counters, and
invalidation reason and time.

Per check: an evidence row with the verdict, a human-readable detail, the structured `observed`
values, and the source verifier name. Evidence is written on **every** attempt, including `error`,
so the audit trail shows the full sequence rather than only the latest state.

`observed` can contain real filesystem paths, git branch and commit values, HTTP statuses, row
counts, and (for `file_hash`) file digests. It does **not** contain file contents or environment
variable values.

## Data retention

**There is no retention limit and no automatic pruning.** Records accumulate for the life of the
data directory. This is intentional — deleting evidence would undermine the audit trail — but it
means the database grows with use.

To clear it deliberately:

```bash
# Stop Hermes first, so no connection holds the database.
rm -rf "<hermes home>/plugin-data/assumption-ledger/"
```

The plugin recreates the directory and schema on next use. **Do not** make deletion your first
troubleshooting step; see [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## Sensitive information handling

### What is deliberately not recorded

- **Environment variable values.** `check_env_set` records only the variable name. When a value
  differs from an expected `equals`, the detail is the fixed string "environment variable differs
  from expected" — the actual value is not written to the evidence row. The docstring states the
  reason: "Do not echo the value: it may be a secret."
- **File contents.** `file_contains` records the pattern that matched, not the matching text.
  `file_hash` records the digest, not the bytes.
- **HTTP response bodies.** Only the status code and, for `http_json_field`, the resolved value at
  the pointer.

### What may be sensitive in the database

- **Source sentences.** Provenance keeps up to 500 characters of the sentence an assumption came
  from. If an agent writes a sentence containing a secret, that text is stored. The plugin does not
  scan for secrets before storing.
- **Filesystem paths**, which can reveal project layout and usernames.
- **The `pointer` value** in `http_json_field` evidence, which is the resolved JSON value. If the
  endpoint returns something sensitive at that pointer, it is stored. Choose pointers accordingly.

### Check specs can name secrets

A check spec is stored verbatim in `check_json`. If you write an `env_set` spec with
`"equals": "<a real secret>"`, that literal is persisted in the database. Prefer checking presence
over equality for anything sensitive.

## Network access

**The plugin makes no network request of its own.** There is no import-time fetch, no update check,
no telemetry endpoint, and no call to the author.

The only outbound traffic is a `http_status` or `http_json_field` check against a URL that appears
in a check spec you recorded. Those requests:

- use `urllib.request` with `method="GET"`;
- send a fixed `User-Agent: assumption-ledger` header and no other header;
- read at most 200,000 bytes of the body;
- honour the spec's `timeout`, defaulting to 5 seconds.

`https` is not enforced; an `http://` URL in a check spec will be fetched over plaintext. Use
`https://` for anything that matters.

## External LLM calls

**The deterministic core makes no LLM call.** This is the central design property, and it is
stated in `__init__.py`'s module docstring: "Everything is deterministic. No LLM call is made to
extract or verify an assumption, so the plugin works with zero provider credentials and cannot be
talked into a verdict."

Verified: `ledger/` imports only `hashlib`, `json`, `os`, `re`, `sqlite3`, `subprocess`, `time`,
`pathlib`, `dataclasses`, `typing` and `urllib`. There is no model client, no HTTP call to an LLM
provider, and no dependency that could make one.

The practical consequence is a security property: **there is no prompt-injection surface in
verification.** An attacker who can influence text the agent sees can at most cause an assumption
to be extracted or not extracted. They cannot argue the verifier into a `pass`.

## Credentials

The plugin requires **none**. `plugin.yaml` declares `requires_env: []`, and the validator confirms
`requires_env: all entries UPPER_SNAKE` (an empty list is trivially valid).

This is a deliberate consequence of the deterministic design: no model call means no API key, and
no network call of its own means no service token. A Hermes deployment with no credentials at all
can run this plugin.

## Permission boundaries

| Capability | Scope |
| --- | --- |
| Read files | Only paths named in check specs, resolved against `CheckContext.root`. |
| Write files | Only inside its own data directory. It does not write to the paths it checks. |
| Run processes | `git` for git checks; the `argv` of a `command_succeeds` spec. |
| Open databases | Read-only, on paths named in `db_*` specs. |
| Open sockets | Only to URLs named in `http_*` specs. |
| Register with Hermes | 4 hooks, 4 tools, 1 slash command. It does not register a context engine, a memory provider, an approval transport, a CLI command, or a platform action. |

**Verified** by the upstream validator: `declared tools: matches registrations`, `declared hooks:
matches registrations`, `declared middleware: matches registrations`, `built-in tool collisions: no
collisions`, `no core override: no runtime rebinds of Hermes core`.

## Known security limitations

These are real. They are documented rather than hidden.

### SQL interpolation in db_row_count

`check_db_row_count` builds its query by string concatenation:

```python
sql = f'SELECT COUNT(*) FROM "{table}"'
if where:
    sql += f" WHERE {where}"
```

The `table` name is validated against `[A-Za-z_][A-Za-z0-9_]*` before it reaches the query, which
is why a spec like `{"table": "a; DROP TABLE b"}` returns `error` rather than executing. The
`where` clause, by contrast, is **not** validated or parameterised. It is passed through to
SQLite verbatim.

The mitigations are real but partial: the connection is opened with `mode=ro`, so writes through
it should fail; and `conn.execute(sql, params)` passes an empty parameter tuple, so there is no
parameter-injection vector. But a read-only connection does not make an arbitrary predicate safe —
`where` can still be used for expensive queries, and read-only enforcement is a SQLite feature
rather than a plugin guarantee.

**Guidance:** treat `where` as trusted input. Do not populate it from model output or from any
source you do not control.

### `command_succeeds` runs an arbitrary argv

The check is documented as "Run a read-only probe command and use its exit code as the verdict",
but nothing enforces read-only. `ctx.runner(argv, ...)` executes whatever `argv` says. There is no
shell involved — `subprocess.run` with a list and no `shell=True` — so shell metacharacters are not
interpreted, but the binary and its arguments are executed with the plugin's privileges.

**Guidance:** only record `command_succeeds` specs with an `argv` you would run yourself. An agent
that can record an arbitrary spec can run an arbitrary command; that is a capability of the
plugin, not a vulnerability in it, but it should be a conscious decision.

### Absolute paths bypass `root`

`CheckContext.resolve(path)` returns an absolute path unchanged:

```python
def resolve(self, path):
    p = Path(path)
    if p.is_absolute() or self.root is None:
        return p
    return self.root / p
```

So a check spec can read any file the process can read, including outside the workspace. The
`root` is a convenience for relative paths, **not** a sandbox boundary.

### No secret scanning before storage

Provenance keeps up to 500 characters of source sentence, and `http_json_field` evidence keeps the
resolved value. Neither is scanned for secrets. If the agent states an assumption containing a
credential, it is stored in plaintext in the SQLite database.

### Unencrypted at rest

The database is plain SQLite. There is no encryption, no password, and no OS keychain integration.
Anyone who can read the file can read every assumption and its evidence. Protect it with
filesystem permissions.

### SQLite filesystem caveats

WAL mode requires a filesystem that supports shared memory. On a network mount this may fail; the
plugin falls back to `DELETE` journal mode rather than erroring. That fallback is correct but
slower and less concurrent. It also means behaviour differs subtly between filesystems, which can
be confusing when the data directory is on a synced or network-backed path.

### No integrity protection on the database

Nothing prevents a local actor from editing `ledger.db` directly — flipping a status, deleting
evidence rows, or removing a decision's dependency. The plugin has no signatures, no hash chain,
and no append-only log. Its records are trustworthy against accidental loss, not against a
determined local adversary.

### Hook failures fail open

Every hook callback catches exceptions and returns `None`. If the engine is unavailable — an
unimportable package, an unwritable data directory — the plugin silently does nothing rather than
blocking the agent. This is the right default for availability, and the wrong one for
enforcement. Log lines are emitted at `WARNING`/`EXCEPTION` level; watch for them if you depend on
the gate.

### The gate can be bypassed by construction

`pre_llm_call` blocks only when a **recorded decision** depends on an invalidated assumption. An
agent that never calls `assumption_decide` is never blocked. The ledger's enforcement is exactly as
strong as the agent's willingness to declare its dependencies.

## Failure modes

| Failure | Effect | Data risk |
| --- | --- | --- |
| Data directory unwritable | `LedgerHost.error` set, engine `None`, plugin no-ops | None |
| `ledger` package unimportable | Same as above | None |
| Database corrupt | Engine construction raises; caught by `LedgerHost`; plugin no-ops | Existing file left as-is |
| WAL unsupported | Falls back to `DELETE` journal mode | None |
| `git` missing | Git checks return `error` → `UNVERIFIED` | None |
| Provider unreachable | `error` → `UNVERIFIED`, or `INVALIDATED` preserved | None |
| Hook raises | Logged, fails open | None |
| `command_succeeds` argv hangs | `subprocess.TimeoutExpired` caught → `error` | None |

In every failure mode the plugin degrades to "did nothing" rather than "corrupted data" or
"blocked the agent".

## Reporting a vulnerability

Report security issues privately, not in a public issue.

1. Open a **private security advisory** on the repository:
   `https://github.com/itzebe/hermes-assumption-ledger/security/advisories/new`
2. If you cannot use advisories, contact the maintainer listed in `plugin.yaml` through GitHub.

Please include: affected version or commit, a description of the issue, reproduction steps, and
the impact you believe it has. If you can, say whether the issue is in the deterministic core
(`ledger/`) or in the Hermes adapter (`__init__.py`) — the core is unit-testable in isolation,
which usually makes a reproducer much faster to confirm.

There is no bug bounty and no formal response SLA. This is a community-tier plugin with a single
maintainer. What you can expect is an acknowledgement, an assessment of whether the report is
correct, and either a fix or a documented rationale for not fixing it.
