# hermes-assumption-ledger — project memory

Hermes Agent plugin (NousResearch Plugin Hackathon, deadline 2026-11-11 17:00 EST).
Canonical repo `github.com/itzebe/hermes-assumption-ledger` (renamed 2026-11-06 from the
misspelled `...-ledge`; the old URL 301-redirects and the SHAs did not change).
Local checkout `/workspace/project`; upstream Hermes reference clone `/tmp/hermes-agent`
(HEAD `486f4a6a9918a4054373e33a200260ffd501645e`, v0.23.0).

## What the plugin is
Deterministic operational-assumption ledger: captures assumptions from observable agent text,
stores provenance, verifies with 12 check kinds, tracks VALID/INVALIDATED/UNVERIFIED/STALE, and
blocks only decisions that depend on a contradicted assumption. **Zero LLM calls** — it cannot be
argued into a verdict and needs no provider credentials.

**The one rule: uncertainty is never an invalidation.** Timeout, HTTP 429/5xx, DNS failure,
missing evidence, unknown check kind → UNVERIFIED (or STALE if fresh evidence existed). Never
INVALIDATED. `status_after_check()` in `ledger/model.py` is the single implementation.

## Repo layout (flat — no src/)
`__init__.py` (Hermes entry, `register(ctx)`) · `plugin.yaml` (manifest v2) · `ledger/`
(`model`, `store`, `verifiers`, `extractor`, `engine`) · `tests/` (5 files, 72 tests).

## Verified Hermes extension facts
- Directory plugin = repo root `plugin.yaml` + `__init__.py` with `register(ctx)`. The loader puts
  the plugin dir on `sys.path` and loads every top-level `*.py` as a submodule, so a sibling
  `ledger/` package imports normally. Entry lives at the **repo root**, not in a subdir.
- `pre_llm_call` → `{"action":"block","message":...}` vetoes before inference;
  `{"context":str}` appends to the user message. Both real (`agent/turn_context.py`).
- `pre_verify` → `{"action":"continue","message":...}` holds an edited-code turn
  (`agent.max_verify_nudges`). Inert otherwise.
- `VALID_MIDDLEWARE` = tool_request, tool_execution, llm_request, llm_execution.
- Durable storage: `plugins.plugin_storage.plugin_data_dir(name)` — survives plugin update/remove.
- `hermes plugins validate <dir>` = 15 checks; `hermes plugins doctor <dir>` loads via real runtime.

## Catalog facts
- `plugin-catalog/<name>.yaml`; `repo` https (GitHub **or GitLab**); `sha` = full 40-hex;
  category ∈ desktop|memory|platform|web|tools|voice|automation|models|general; contributor PRs
  are forced to `tier: community`. `subdir:` is allowed (plain relative path).
- 1064 entries at HEAD. `assumption-ledger` is free; no entry mentions "assumption". The
  `*-ledger` name space is crowded but unrelated. Closest: `epistemic-harness` (needs
  TYPESAFE_API_KEY, external LLM), `agent-ledger` (side-effect WAL + OWASP scorer).

## Verification status
72 tests green. 12 frozen oracles in `tests/test_frozen_oracles.py` cover the 12 adversarial
cases; verified **non-vacuous** (injecting ERROR→INVALIDATED fails 4). `hermes plugins validate`
all 15 checks pass; doctor reports 4 tools + 4 hooks. Local commit `d398e2c` on `master`.

## Published state (2026-11-06)
- Live at **`github.com/itzebe/hermes-assumption-ledger`** (canonical). The original misspelled
  name `...-ledge` 301-redirects; the rename did **not** change any commit SHA.
- MIT, public, default branch `main`. Current pin **`650533a22434c330e6a0fa8f2ee7bb3147b5c029`**
  (v0.2.0) on both `main` and branch `docs/complete-project-documentation`. 284 tests + 1 skip.
  Previous pin `388cf029…` (v0.1.1, 73 tests) is what the maintainer reviewed.
- Catalog PR **`NousResearch/hermes-agent#150212`**; its head branch
  `feat/assumption-ledger-plugin-catalog` lives in **`itzebe/hermes-agent`** (the fork), so
  pushing that branch from `/workspace/pr-work/fork` updates the PR diff.
- Upstream `main` moves fast. **Always re-check the base and re-pin before opening the PR.**

## Hermes plugin rules learned the hard way
- The `hermes plugins validate` **security scan reads test fixtures and scripts**, not just
  runtime code. A literal `file:///etc/passwd` in a test corpus fails validation as critical
  (`tools/skills_guard.py`). Use neutral paths in fixtures.
- **Pinned-SHA validation is format-only.** The catalog test accepts a well-formed but
  nonexistent SHA (all-ones passes; all-zeros fails only because YAML parses it as int `0`).
  Verify a pin by cloning and checking out the exact commit, never by that test.
- A standalone plugin **cannot import core**; but core's helpers (e.g.
  `tools/url_safety.is_safe_url`) are the reference for behaviour. Reimplement, then *prove*
  parity with a script — see `scripts/core_parity.py` (35 cases, 0 mismatches at core `ff6781e8e1`).
- `hermes plugins doctor` reports declared tools/hooks; `validate` checks declared vs registered
  match, tool collisions and the security scan.

## The rule the whole plugin rests on
`status_after_check` (ledger/model.py): pass→VALID, fail→INVALIDATED, error→STALE/UNVERIFIED.
**An error must leave an existing INVALIDATED untouched** — otherwise a provider outage silently
unblocks contradicted work. The old code returned STALE when fresh evidence existed, and the
oracle only covered the `unsupported` branch (which returns before the rule), so it hid the bug.
Also `git_clean` must not count untracked files by default (the ledger's own DB made the tree
look dirty).

## Blocker — needs the user
`GITHUB_TOKEN` is a GitHub App installation token with **empty `x-oauth-scopes`**. It **can** push
to existing `itzebe` repos (incl. the `itzebe/hermes-agent` fork) but every write to
`NousResearch/hermes-agent` is 403 "Resource not accessible by integration": `POST /user/repos`,
`PATCH /repos/...`, `POST /repos/<up>/forks`, `POST /repos/<up>/pulls`, and also
`PATCH .../pulls/<n>` and `POST .../issues/<n>/comments`. So PR **body edits and review-response
comments must be pasted by the owner**. `GITLAB_TOKEN` is invalid (401).
