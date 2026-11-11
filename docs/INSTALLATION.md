# Installation

How to install, verify, update and remove Assumption Ledger.

- [Prerequisites](#prerequisites)
- [Which install method should I use?](#which-install-method-should-i-use)
- [Method 1: install from the official catalog](#method-1-install-from-the-official-catalog)
- [Method 2: install from this repository](#method-2-install-from-this-repository)
- [Verifying the installation](#verifying-the-installation)
- [Activation](#activation)
- [Dependencies](#dependencies)
- [Configuration](#configuration)
- [Storage location](#storage-location)
- [Updating](#updating)
- [Uninstalling](#uninstalling)
- [Catalog status](#catalog-status)

## Prerequisites

| Requirement | Detail |
| --- | --- |
| Hermes Agent | `>=0.23.0` (declared as `requires_hermes` in `plugin.yaml`) |
| Python | `>=3.11` (declared in `pyproject.toml`) |
| Git | Only needed if you clone, and for the `git_*` checks |
| Third-party packages | **None.** `dependencies = []`; standard library only. |
| Credentials | **None.** `requires_env: []`. |

The plugin needs no API key, no token and no service account. That follows from its design: no
model call, and no network request of its own.

## Which install method should I use?

There are two, and they are **not** equivalent.

| | Method 1: official catalog | Method 2: this repository |
| --- | --- | --- |
| Status | **Available now** | Available, but a custom source |
| Identifier | `assumption-ledger` | `itzebe/hermes-assumption-ledger` |
| Installs | The reviewed pinned SHA from the catalog (`07bfaaa…`, v0.2.2) | The repository's **default branch** (`main`, still v0.2.0) |
| Review | Catalog review plus a pinned SHA | None beyond the upstream validator; the install-time scan may refuse it |
| Recommended | **Yes** | Only for inspecting or pinning an unreleased revision |

**Use Method 1.** The bare name `assumption-ledger` resolves through the official catalog, which
carries the reviewed code and the security fixes. The `owner/repo` form clones the default branch
(which is still the older `v0.2.0`) and Hermes flags it as an **unreviewed custom source** — its
install-time scan may block it with `Decision: BLOCKED`.

## Method 1: install from the official catalog

```bash
hermes plugins install assumption-ledger
```

Add `--enable` to skip the "Enable now?" prompt in a non-interactive session:

```bash
hermes plugins install assumption-ledger --enable
```

Hermes resolves the bare name through the catalog, prints the reviewed entry and its pin, and
clones that exact commit. The catalog is cached locally, so a version published very recently can
take a short while to appear; run `hermes plugins search assumption-ledger` to see the version the
local catalog currently offers.

## Method 2: install from this repository

Hermes accepts a Git URL or an `owner/repo` shorthand. Both were verified to resolve:

| Identifier | Resolves to |
| --- | --- |
| `https://github.com/itzebe/hermes-assumption-ledger` | the repository |
| `https://github.com/itzebe/hermes-assumption-ledger.git` | the repository |
| `itzebe/hermes-assumption-ledger` | `https://github.com/itzebe/hermes-assumption-ledger.git` |

### Install

```bash
hermes plugins install itzebe/hermes-assumption-ledger
```

Add `--enable` to skip the "Enable now?" prompt in a non-interactive session:

```bash
hermes plugins install itzebe/hermes-assumption-ledger --enable
```

### Or clone and validate before installing

Useful if you want to inspect the code, or run the test suite first:

```bash
git clone https://github.com/itzebe/hermes-assumption-ledger
cd hermes-assumption-ledger
hermes plugins validate .
hermes plugins doctor .
```

Then install from the local checkout by path, or install by identifier as above.

### Pin a specific revision

```bash
hermes plugins install itzebe/hermes-assumption-ledger --ref <40-character-commit-sha>
```

`--ref` takes exactly one immutable **40-character** commit SHA. The catalog pins
`07bfaaaeb58fed1fa2d717f2c201e7bb17a61965` (v0.2.2), the commit the maintainer reviewed, so pinning
that value gives you exactly the reviewed code:

```bash
hermes plugins install itzebe/hermes-assumption-ledger \
  --ref 07bfaaaeb58fed1fa2d717f2c201e7bb17a61965
```

An explicit `--ref` makes the install a **custom pin**: it is no longer the catalog's reviewed
entry, and the install-time scan applies as for any unreviewed source.

## Verifying the installation

### 1. Run the validator

```bash
hermes plugins validate <path-to-plugin-dir>
```

Expected result — 15 checks, all passing, zero warnings:

```
manifest                     plugin.yaml parses
manifest fields              name, version, description present
requires_hermes              spec '>=0.23.0' parses
config schema                shape valid
requires_env                 all entries UPPER_SNAKE
oauth                        not declared
loadable                     entry: __init__.py
python dependencies          none declared
capability probe             register() ran in isolation
declared tools               matches registrations
declared hooks               matches registrations
declared middleware          matches registrations
built-in tool collisions     no collisions
security scan                safe
no core override             no runtime rebinds of Hermes core
```

The `capability probe` check is the interesting one: Hermes actually runs `register()` in
isolation and confirms that the declared tools and hooks are exactly what gets registered.

### 2. Run the doctor

```bash
hermes plugins doctor <path-to-plugin-dir>
```

Expected result:

```
Plugin Doctor: <path>
  manifest: assumption-ledger 0.2.2 (standalone)
  OK: runtime discovery, manifest parsing, import, and registration passed
  registrations: 6 tool(s), 4 hook(s)
```

**6 tools and 4 hooks** is the correct count. If you see fewer, something failed to register.

### 3. Confirm the tools are available

```bash
hermes plugins capabilities assumption-ledger
```

Or ask the agent to call `assumption_status`. A healthy plugin returns:

```json
{"total": 0, "by_status": {"VALID": 0, "INVALIDATED": 0, "UNVERIFIED": 0, "STALE": 0},
 "invalidated": [], "unverified": [], "stale": [], "blocked_decisions": [],
 "supported_check_kinds": ["command_succeeds", "db_row_count", "db_table_exists", "env_set",
   "file_contains", "file_exists", "file_hash", "git_branch", "git_clean", "git_commit",
   "http_json_field", "http_status"]}
```

### 4. Confirm the slash command

In a Hermes session:

```
/assumptions
```

Expected on a fresh install:

```
Assumption Ledger
  tracked: 0  valid=0  invalidated=0  unverified=0  stale=0
  (nothing captured yet)
```

### 5. Confirm the storage directory appears

It is created on first use, not at install time:

```bash
ls "<hermes home>/plugin-data/assumption-ledger/"
# ledger.db
```

## Activation

If you did not pass `--enable` at install time:

```bash
hermes plugins enable assumption-ledger
```

To check what is enabled:

```bash
hermes plugins list
```

Hooks take effect in new sessions. If you installed while a session was running, start a new one.

## Dependencies

**There are none.** `pyproject.toml` declares `dependencies = []`, and the validator reports
`python dependencies: none declared`. Nothing is installed alongside the plugin, and no
`pip install` step is needed.

The only optional extra is the test toolchain:

```bash
pip install -e ".[dev]"    # installs pytest>=8.0
```

## Configuration

There is nothing to configure for normal use. The manifest declares five options, and all five are
read at runtime through the sanctioned `PluginContext.get_config` seam:

| Key | Default | Meaning |
| --- | --- | --- |
| `freshness_seconds` | `86400` | How long a pass stays `VALID` before it goes `STALE`. |
| `auto_check` | `false` | Verify an assumption as soon as it is recorded. |
| `max_reinject` | `25` | Maximum live assumptions re-injected per turn. |
| `command_allowlist` | `[]` | Executable names `command_succeeds` may run. Empty disables command checks. |
| `allow_private_urls` | `false` | Permit HTTP checks to reach private/loopback hosts (metadata stays blocked). |

A malformed value falls back to the default rather than disabling the plugin.
`/assumptions config` prints the effective values, and
[tests/test_config.py](../tests/test_config.py) asserts an observable effect for each setting. See
[CONFIGURATION.md](CONFIGURATION.md) for the full reference.

## Storage location

| Order | Condition | Path |
| --- | --- | --- |
| 1 | Hermes runtime available | `<hermes home>/plugin-data/assumption-ledger/` |
| 2 | `HERMES_HOME` set | `$HERMES_HOME/plugin-data/assumption-ledger/` |
| 3 | Neither | `~/.hermes/plugin-data/assumption-ledger/` |

This is the sanctioned plugin-data seam, which is why records survive both `hermes plugins update`
and `hermes plugins remove`. Storing state in the plugin's install directory would lose it on
update, because that directory is git-pulled.

Files: `ledger.db`, plus `ledger.db-wal` and `ledger.db-shm` while the connection is open.

## Updating

```bash
hermes plugins update assumption-ledger
```

Records are unaffected: they live under `plugin-data/`, not in the install directory. You can also
enable automatic updates:

```bash
hermes plugins auto-update on
```

Because the plugin has no dependencies, an update cannot break your environment by pulling a new
package.

## Uninstalling

```bash
hermes plugins disable assumption-ledger
hermes plugins remove  assumption-ledger
```

**This removes the code but leaves your data.** The plugin data directory is not deleted, so a
later reinstall picks up where you left off. To remove the data as well, delete it deliberately —
after stopping Hermes so no connection holds the database:

```bash
rm -rf "<hermes home>/plugin-data/assumption-ledger/"
```

Deletion is not a troubleshooting step. See [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for what to do
when the plugin misbehaves — the ledger is usually the evidence you need, not the cause.

## Catalog status

The plugin **is in the official Hermes plugin catalog**. The upstream pull request is merged:

- Repository: `NousResearch/hermes-agent`
- Pull request: [#150212](https://github.com/NousResearch/hermes-agent/pull/150212) — **merged**
- Entry: `plugin-catalog/assumption-ledger.yaml`
- Version: `0.2.2`
- Pinned commit: `07bfaaaeb58fed1fa2d717f2c201e7bb17a61965`
- Maintainer: `itzebe` · Tier: `community` · Category: `memory`
- Requires: `hermes >=0.23.0` · 6 tools · 4 hooks · no env vars

Verify the listing yourself against the upstream repository, not this document:

```bash
hermes plugins search assumption-ledger     # shows the catalog version and pin
hermes plugins info   assumption-ledger     # shows the pin, tools, hooks, maintainer
```

The pinned commit `07bfaaa…` is an ancestor of the default branch, so it stays reachable and
installable by `--ref` even as `main` moves forward.

**Honest limitations.** The repository's default branch (`main`) is still `v0.2.0`; the catalog pin
carries the security fixes `main` does not yet have, so the two install methods are not
equivalent. A catalog install is served from a locally cached catalog, so a version published very
recently can take a short while to appear — `hermes plugins search` reports what the cache holds.
