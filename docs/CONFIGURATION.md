# Configuration

This document covers the manifest configuration schema, the check specifications you write into
assumptions, environment variables, and storage paths.

**Read [Declared configuration options are not currently read at runtime](#declared-configuration-options-are-not-currently-read-at-runtime)
before you try to change any plugin setting.** It describes a real gap between the manifest and the
implementation.

- [Declared configuration options](#declared-configuration-options)
- [Declared configuration options are not currently read at runtime](#declared-configuration-options-are-not-currently-read-at-runtime)
- [Check specifications](#check-specifications)
- [Environment variables](#environment-variables)
- [Storage paths](#storage-paths)
- [Configuration precedence](#configuration-precedence)

## Declared configuration options

`plugin.yaml` declares a `config_schema` with three options:

| Option | Type | Declared default | Declared meaning |
| --- | --- | --- | --- |
| `freshness_seconds` | int | `86400` | How long a successful check stays `VALID` before it degrades to `STALE` and needs re-checking. A pass is never treated as permanently true. |
| `auto_check` | bool | `true` | Re-verify `VALID` assumptions whose evidence has aged out when the plugin next runs a check. |
| `max_reinject` | int | `25` | Maximum live assumptions re-injected into context on each turn. |

Hermes validates this schema. Its loader calls `validate_config_schema(...)` and **logs a warning**
if a value under `plugins.entries.<plugin-id>.settings` violates the declared type — it never
blocks the load, and it does not inject defaults.

## Declared configuration options are not currently read at runtime

This is the honest state of the implementation, and it is a limitation rather than a feature.

**None of the three declared options is read by any code path.** The behaviour they describe is
present, but the values are compiled-in constants rather than settings:

| Declared option | What actually happens | Where |
| --- | --- | --- |
| `freshness_seconds` | The engine is constructed with `DEFAULT_FRESHNESS_SECONDS = 24 * 60 * 60`, i.e. exactly the declared default of 86400. Nothing reads a configured value. | `ledger/engine.py` |
| `max_reinject` | `on_pre_llm_call` calls `engine.render_reinjection()` with no argument, so the parameter default `limit=25` applies — again matching the declared default. | `__init__.py`, `ledger/engine.py` |
| `auto_check` | There is no code path for this option at all. `sweep_stale()` is unconditional; it is called from `summary()` and `on_pre_verify`. | `ledger/engine.py` |

### How this was verified

```bash
# No configuration is read anywhere in the plugin:
grep -rn "get_config\|ctx.config\|register_config" --include=*.py .
# -> no matches
```

The adapter never calls Hermes' `ctx.get_config(...)`, and no module reads `freshness_seconds`,
`auto_check` or `max_reinject` from configuration. The only occurrences of those identifiers are
the constructor parameter and the module constant, both with the same value as the declared
default.

### What this means for you

- **You cannot currently tune these three values** by editing Hermes configuration. Setting them
  will validate but have no effect on behaviour.
- **The effective behaviour matches the declared defaults exactly**, so the documented defaults are
  accurate descriptions of what the plugin does. Nothing is silently different from the manifest.
- **If you need a different freshness window**, the supported route today is to use the engine
  directly (`LedgerEngine(store, freshness_seconds=...)`) or to embed it, as the tests do. The
  Hermes plugin surface does not expose it.
- **Treat `auto_check: false` as unimplemented.** You cannot turn the stale sweep off.

This gap is recorded rather than silently patched, because fixing it would be a behaviour change
and this is a documentation change. See [CHANGELOG.md](../CHANGELOG.md#known-issues) and
[CONTRIBUTING.md](../CONTRIBUTING.md).

### A related inconsistency

`__init__.py` defines `__version__ = "0.1.0"`, while `plugin.yaml`, `pyproject.toml` and
`ledger/__init__.py` all declare `0.1.1`. The plugin's **effective** version is `0.1.1`: that is
what Hermes reads from the manifest (and what `hermes plugins doctor` reports), and what
`ledger.__version__` returns. The `0.1.0` string in the adapter module is a stale leftover. If you
import the version for a compatibility check, read `plugin.yaml` or `ledger.__version__`, not the
adapter module.

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

| Kind | Field | Type | Required | Notes |
| --- | --- | --- | --- | --- |
| `command_succeeds` | `argv` | array of strings | yes | Must be non-empty and all strings. Executed directly — no shell. |
| | `cwd` | string | no | Default `"."`. |
| | `timeout` | number | no | Defaults to `ctx.default_timeout`. |

Exit code 0 is a `pass`; any non-zero exit is a `fail`. This is documented as a read-only probe;
the plugin does not enforce that. See [SECURITY.md](SECURITY.md#command_succeeds-runs-an-arbitrary-argv).

### HTTP

| Kind | Field | Type | Required | Notes |
| --- | --- | --- | --- | --- |
| `http_status` | `url` | string | yes | |
| | `expected_status` | int or list | no | Default `200`. A list means "any of these". |
| | `timeout` | number | no | Defaults to `ctx.default_timeout`. |
| `http_json_field` | `url` | string | yes | |
| | `pointer` | string | yes | Dotted path, e.g. `"a.b.0.c"`. This is not RFC 6901 JSON Pointer. |
| | `equals` | any | no | When omitted, mere presence of the pointer is a `pass`. |
| | `timeout` | number | no | |

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
| | `where` | string | no | Interpolated verbatim into the SQL. Only use values you control. |
| | `equals` | int | no | Exact count. |
| | `min` | int | no | Lower bound. |
| | `max` | int | no | Upper bound. |

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
