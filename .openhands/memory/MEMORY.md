# hermes-assumption-ledger — project memory

Hermes Agent plugin (NousResearch Plugin Hackathon, deadline 2026-11-11 17:00 EST).
Target repo `github.com/itzebe/hermes-assumption-ledger` (does not exist yet — see Blocker).
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

## Blocker — needs the user
`GITHUB_TOKEN` is fine-grained with **empty x-oauth-scopes**: `POST /user/repos` and
`gh repo create` both 403 "Resource not accessible by integration", so the target repo cannot be
created. `GITLAB_TOKEN` is invalid (401). The token *can* push to the 15 existing `itzebe` repos
(all admin=true). Unblock by creating `itzebe/hermes-assumption-ledger` and granting Contents +
Administration write, or ship the plugin as a `subdir:` of an existing repo.

## Process
- Hackathon entry = catalog PR to `NousResearch/hermes-agent` + demo video on X tagging
  @NousResearch. Judged on usefulness, reliability, creativity.
- PRs from this environment open as drafts; `gh pr ready <n>` before merge.
