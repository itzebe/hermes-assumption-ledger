# Configuration

This document covers the manifest configuration schema, the check specifications you write into
assumptions, environment variables, and storage paths.

Every declared option is read at runtime through the sanctioned `PluginContext.get_config` seam.
[`tests/test_config.py`](../tests/test_config.py) asserts that each one has an observable effect,
so a setting that stops being wired fails the suite.

- [Declared configuration options](#declared-configuration-options)
- [How the values are read](#how-the-values-are-read)
- [Check specifications](#check-specifications)
- [Environment variables](#environment-variables)
- [Storage paths](#storage-paths)
- [Configuration precedence](#configuration-precedence)

## Declared configuration options

`plugin.yaml` declares a `config_schema` with five options:

| Option | Type | Default | Meaning |
| --- | --- | --- | --- |
| `freshness_seconds` | int | `86400` | How long a successful check stays `VALID` before it degrades to `STALE` and needs re-checking. A pass is never treated as permanently true. Bounded to 1..31536000. |
| `auto_check` | bool | `false` | Verify an assumption immediately when it is recorded, instead of leaving it `UNVERIFIED` until a check is requested. Off by default: it makes capture perform I/O. |
| `max_reinject` | int | `25` | Maximum live assumptions re-injected into context on each turn. Bounded to 1..200. |
| `command_allowlist` | list | `[]` | Executable names `command_succeeds` may run. Empty disables command checks entirely. |
| `allow_private_urls` | bool | `false` | Permit HTTP checks to reach loopback, private, link-local and CGNAT hosts. Cloud-metadata endpoints stay blocked regardless. |

Hermes validates this schema. Its loader calls `validate_config_schema(...)` and **logs a warning**
if a value under `plugins.entries.<plugin-id>.settings` violates the declared type — it never
blocks the load, and it does not inject defaults.

## How the values are read

At registration the adapter captures `ctx.get_config` and stores it in `_CONFIG_GETTER`. On first
use it resolves the effective configuration once and caches it on the host:

| Option | Effect | Where |
| --- | --- | --- |
| `freshness_seconds` | Passed to `LedgerEngine(freshness_seconds=...)`; bounds the `VALID` -> `STALE` window used by `sweep_stale`. | `__init__.py`, `ledger/engine.py` |
| `auto_check` | Passed to `LedgerEngine(auto_check_on_capture=...)`; when true, capture and `add_assumption` verify immediately. | `__init__.py`, `ledger/engine.py` |
| `max_reinject` | Passed to `LedgerEngine(reinject_limit=...)`; caps the live-assumption lines in `render_reinjection`. | `__init__.py`, `ledger/engine.py` |
| `command_allowlist` | Becomes `CheckContext.command_allowlist`; empty means `command_succeeds` returns `error` without executing. | `ledger/command_policy.py`, `ledger/verifiers.py` |
| `allow_private_urls` | Becomes `CheckContext.allow_private_urls`; cloud-metadata endpoints stay blocked either way. | `ledger/url_safety.py`, `ledger/verifiers.py` |

Coercion is defensive. A value that will not parse falls back to the default, an out-of-range
integer is clamped, and a config-backend exception logs and uses the safe defaults rather than
disabling the plugin. `/assumptions config` prints the effective values.

Verify the wiring yourself:

```bash
# Every declared key is named in the implementation, and every key the code reads is declared:
python3 -m pytest tests/test_config.py -q
```

## Check specifications

A check spec is a JSON object whose `kind` selects the verifier. These are the real fields each
verifier reads. Anything not listed is ignored.

### Filesystem

| Kind | Field | Type | Required | Notes |
| --- | --- | --- | --- | --- |
| `file_exists` | `path` | string | yes | Relative paths resolve against `CheckContext.root`. |
| `file_contains` | `path` | string | yes | |
| | `pattern` | string | yes | Python regular expression, compiled with `re.MULTILINE`. |
| | `ignore_case` | bool | no | Adds `re.IGNORECASE`. Default `false`. |
| `file_hash` | `path` | string | yes | |
| | `sha256` | string | yes | Compared case-insensitively. |
| | `algo` | string | no | Any `hashlib` algorithm name. Default `"sha256"`. Note the field is named `sha256` even when `algo` is changed. |

Example:

```json
{"kind": "file_contains", "path": "config/prod.json", "pattern": "\"region\"", "ignore_case": true}
```

### Git

All git checks accept `cwd` (default `"."`, resolved against `root`) and use `ctx.default_timeout`.

| Kind | Field | Type | Required | Notes |
| --- | --- | --- | --- | --- |
| `git_branch` | `branch` | string | yes | Exact match against `git rev-parse --abbrev-ref HEAD`. |
| `git_commit` | `commit` | string | yes | Matched with `startswith`, so a short SHA works. |
| `git_clean` | `include_untracked` | bool | no | Default `false`. When `false`, untracked files are reported in `observed.untracked` but do not fail the check. |

Example:

```json
{"kind": "git_clean", "cwd": ".", "include_untracked": false}
```

### Environment

| Kind | Field | Type | Required | Notes |
| --- | --- | --- | --- | --- |
| `env_set` | `name` | string | yes | Read from `ctx.env` (defaults to `os.environ`). |
| | `equals` | string | no | When present, the value must match. A mismatch is a `fail`, and the value is **not** recorded. |

### Commands

Command execution is **disabled by default**. It requires a non-empty `command_allowlist` in the
plugin configuration; until then every `command_succeeds` check returns `error` without running
anything.

| Kind | Field | Type | Required | Notes |
| --- | --- | --- | --- | --- |
| `command_succeeds` | `argv` | array of strings | yes | Must be non-empty and all strings. Executed directly — no shell. |
| | `cwd` | string | no | Default `"."`. |
| | `timeout` | number | no | Defaults to `ctx.default_timeout`. |

The executable's basename must appear in `command_allowlist`, or the check returns `error`. A check
spec cannot add to the allowlist. The child process gets a minimal environment (`PATH`, empty
`HOME`, empty `TMPDIR`, `LC_ALL=C`, `LANG=C`), so a tool cannot read the host's credentials.

Exit code 0 is a `pass`; any non-zero exit is a `fail`. A missing binary, a policy refusal or a
timeout is an `error`. See [SECURITY.md](SECURITY.md#command-execution-policy).

### HTTP

Every URL is checked against the SSRF policy before the request is made, and each redirect hop is
re-checked. Loopback, private, link-local, CGNAT and reserved addresses are refused; cloud-metadata
endpoints are refused even when `allow_private_urls` is true. A refusal is an `error`, not a `fail`.

| Kind | Field | Type | Required | Notes |
| --- | --- | --- | --- | --- |
| `http_status` | `url` | string | yes | |
| | `expected_status` | int or list | no | Default `200`. A list means "any of these". |
| | `timeout` | number | no | Defaults to `ctx.default_timeout`. |
| `http_json_field` | `url` | string | yes | |
| | `pointer` | string | yes | Dotted path, e.g. `"a.b.0.c"`. This is not RFC 6901 JSON Pointer. |
| | `equals` | any | no | When omitted, mere presence of the pointer is a `pass`. |
| | `timeout` | number | no | |

Only `http` and `https` are accepted. A 4xx is a `fail`; a 5xx, a 429, a timeout or a transport
error is an `error`. Response bodies are bounded at 200 KB. See
[docs/SECURITY.md](SECURITY.md#http-checks-and-ssrf).

Example:

```json
{"kind": "http_status", "url": "https://example.com/health", "expected_status": [200, 204]}
```

```json
{"kind": "http_json_field", "url": "https://example.com/api", "pointer": "release.commit", "equals": "486f4a6a"}
```

### SQLite

Both open the database read-only (`mode=ro`).

| Kind | Field | Type | Required | Notes |
| --- | --- | --- | --- | --- |
| `db_table_exists` | `path` | string | yes | |
| | `table` | string | yes | Exact match against `sqlite_master`. |
| `db_row_count` | `path` | string | yes | |
| | `table` | string | yes | Must match `[A-Za-z_][A-Za-z0-9_]*`; otherwise `error`. |
| | `where` | string | no | Validated against a simple-predicate grammar before use. Anything else is an `error`. |
| | `equals` | int | no | Exact count. |
| | `min` | int | no | Lower bound. |
| | `max` | int | no | Upper bound. |

The `where` clause accepts one or more `column OP literal` atoms joined by `AND`/`OR`, with `OP` one
of `=`, `!=`, `<>`, `<`, `<=`, `>`, `>=`, plus `IS NULL` / `IS NOT NULL`. Subqueries, function
calls, comments and semicolons are refused. A refused clause returns `error`, which never
invalidates an assumption.

If none of `equals`/`min`/`max` is given, any count passes.

### Unsupported

`{"kind": "unsupported"}` is assigned by the extractor to an explicit marker with no checkable
form. It is handled by the engine, not by a verifier: the record stays `UNVERIFIED` and each check
writes an `error` evidence row.

### Invalid examples

These are accepted by the schema but always produce `error`, which is the intended behaviour:

```json
{"kind": "file_exists"}                                  // no path -> error
{"kind": "not_a_real_kind"}                              // unknown kind -> error
{"kind": "db_row_count", "table": "a; DROP TABLE b"}     // unsafe table name -> error
{"kind": "http_status", "url": "not-a-url"}              // transport error -> error
```

## Environment variables

The plugin reads two environment variables, both only as storage fallbacks:

| Variable | Read by | Effect |
| --- | --- | --- |
| `HERMES_HOME` | `_data_dir()` | Base directory for the data dir when Hermes' own `plugin_data_dir` is not importable. |
| — | — | — |

There is no `HERMES_ASSUMPTION_LEDGER_*` variable. No credential, token or API key is read by any
code path. `plugin.yaml` declares `requires_env: []`.

`HERMES_HOME` is also the variable the integration tests use to keep the plugin's data out of your
real profile:

```python
monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
```

## Storage paths

`_data_dir()` resolves in this order:

| Order | Condition | Path |
| --- | --- | --- |
| 1 | Hermes runtime importable | `<hermes home>/plugin-data/assumption-ledger/` via `plugins.plugin_storage.plugin_data_dir` |
| 2 | `HERMES_HOME` set | `$HERMES_HOME/plugin-data/assumption-ledger/` |
| 3 | Neither | `~/.hermes/plugin-data/assumption-ledger/` |

The database is `ledger.db` inside that directory. WAL mode creates sibling files:

```
<data dir>/ledger.db          # the database
<data dir>/ledger.db-wal      # write-ahead log (present while the connection is open)
<data dir>/ledger.db-shm      # shared-memory index
```

The directory is created on first use. The plugin never prunes it; see
[SECURITY.md](SECURITY.md#data-retention) for how to clear it deliberately.

Verified on a real run: with `HERMES_HOME` set to a temp directory, the plugin created
`<tmp>/plugin-data/assumption-ledger/ledger.db` and the file existed after the first tool call.

## Configuration precedence

There are effectively **two** layers, not a general precedence chain:

1. **The check spec on the assumption.** Highest authority for verification behaviour. It decides
   which verifier runs and with what parameters. It cannot be overridden.
2. **`CheckContext` defaults.** `root`, `cwd`, `env`, `default_timeout`. These supply values a spec
   omits. When embedded, the caller sets them; under Hermes, the adapter resolves `root`/`cwd` from
   the agent's working directory.

Plugin settings under `plugins.entries.<plugin-id>.settings` are **not** a layer, because nothing
reads them (see [above](#declared-configuration-options-are-not-currently-read-at-runtime)).

For direct embedding, precedence is simply: explicit `CheckContext` argument, else the built-in
default in `CheckContext.__init__`.
