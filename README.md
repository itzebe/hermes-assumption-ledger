# Assumption Ledger

A persistent operational-assumption ledger for Hermes Agent.

An agent silently builds on things it has not checked: a file it believes exists, a branch it
believes it is on, a deployment it believes is live, a table it believes has rows. When one of
those beliefs is wrong, the failure surfaces much later and somewhere else. Assumption Ledger
captures those beliefs as first-class records, keeps their provenance, verifies them with
deterministic checks, and stops only the work that actually depends on a belief that has been
contradicted.

Everything is deterministic. No LLM call is made to extract or verify an assumption, so the
plugin needs no provider credentials and cannot be argued into a verdict.

## What it does

1. **Capture.** Assumptions come from explicit markers (`ASSUMPTION:`, `ASSUME:`,
   `PRECONDITION:`) in assistant messages and tool results, and from a small set of anchored
   natural-language patterns ("the file X exists", "on branch B", "HEAD is at commit C",
   "https://… returns 200"). Each record keeps the sentence it came from, so extraction is
   auditable rather than trusted.
2. **Verify.** Each assumption carries a deterministic check. Twelve check kinds are supported:
   `file_exists`, `file_contains`, `file_hash`, `git_branch`, `git_commit`, `git_clean`,
   `env_set`, `command_succeeds`, `http_status`, `http_json_field`, `db_table_exists`,
   `db_row_count`.
3. **Track lifecycle.** Records move between `VALID`, `INVALIDATED`, `UNVERIFIED` and `STALE`.
4. **Propagate.** A decision that depends on an invalidated assumption is marked `blocked`. A
   pre-LLM gate stops the turn only when such a dependency exists, and a verify-gate holds
   edited-code turns the same way. Unrelated work is never blocked.
5. **Survive.** Records live in SQLite under the plugin data directory, so they outlive a
   compaction, a restart, and `hermes plugins update`.

## The one rule that matters

> Uncertainty is never an invalidation.

A provider timeout, an HTTP 429 or 5xx, a DNS failure, a missing file, a rate limit or an
unknown check kind produce `UNVERIFIED` (or `STALE` if we previously held fresh evidence). They
never produce `INVALIDATED`. Only a definitive contradiction does. This is the difference
between a ledger you can trust and a false-alarm generator.

## Lifecycle

| Status | Meaning |
| --- | --- |
| `VALID` | Evidence supports the assumption and is inside the freshness window. |
| `INVALIDATED` | Evidence definitively contradicts it. Dependent decisions are blocked. |
| `UNVERIFIED` | No usable evidence yet. The safe default. |
| `STALE` | Was valid; evidence aged out or could not be refreshed. Needs re-checking. |

A pass is never assumed to hold forever: `freshness_seconds` (default 86400) bounds how long
`VALID` lasts before degrading to `STALE`.

## Install

From the catalog (once listed):

```
hermes plugins install assumption-ledger
```

From this repository:

```bash
git clone https://github.com/itzebe/hermes-assumption-ledger
hermes plugins validate ./hermes-assumption-ledger
hermes plugins doctor  ./hermes-assumption-ledger
```

Then enable it and run Hermes as usual. Records are written to
`<hermes home>/plugin-data/assumption-ledger/ledger.db`.

## Surfaces

**Tools** — `assumption_record`, `assumption_check`, `assumption_status`, `assumption_decide`.

**Hooks** — `pre_llm_call` (re-inject live assumptions; block on a contradicted dependency),
`post_llm_call` (capture), `post_tool_call` (capture explicit markers), `pre_verify` (hold a
turn while a contradicted assumption is load-bearing).

**Slash command** — `/assumptions [status|list|check [id]|show <id>]`.

## Example

```
ASSUMPTION: the file config/prod.json exists
ASSUMPTION: HEAD is at commit 486f4a6a9918a4054373e33a200260ffd501645e
```

The agent then records a decision:

```json
{"statement": "deploy the release", "depends_on": ["asm_...", "asm_..."]}
```

If `config/prod.json` turns out not to exist, that assumption becomes `INVALIDATED`, the deploy
decision is marked `blocked` with a reason naming it, and the next turn stops with an
explanation. If instead the check merely timed out, the assumption stays `UNVERIFIED` and the
turn proceeds.

## Configuration

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `freshness_seconds` | int | 86400 | How long a pass stays `VALID` before it goes `STALE`. |
| `auto_check` | bool | true | Re-verify aged-out assumptions on the next check run. |
| `max_reinject` | int | 25 | Maximum live assumptions re-injected per turn. |

## Privacy

Everything is local. The plugin makes no network request of its own; the only outbound traffic
is the HTTP checks you explicitly record (`http_status`, `http_json_field`) against URLs you
name. Nothing is sent to the plugin author. Records are plain SQLite under
`<hermes home>/plugin-data/assumption-ledger/` and are yours to inspect or delete.

## Tests

```
python -m pytest
```

72 tests, including twelve frozen oracles covering the adversarial cases: correct evidence,
contradictory evidence, missing evidence, timeout, rate limit, staleness, propagation,
restart recovery, compaction re-injection, conflicting evidence, malformed extraction, and
duplicate detection. The oracle suite is verified non-vacuous: reverting the
uncertainty-never-invalidates rule fails four of them.

## License

MIT
