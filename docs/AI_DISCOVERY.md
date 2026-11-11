# AI Discovery

How this repository is made discoverable to AI assistants, code-search tools and search engines, and
how to verify each surface yourself. This document describes what the project controls and — just as
importantly — what it does not.

## What this document is not

- It is not a claim that any particular assistant has indexed, crawled or recommended this project.
  Those are third-party behaviours nobody here controls, and none are asserted.
- It is not keyword manipulation. Every claim is a factual statement about the source.
- It does not add a `robots.txt`. GitHub controls its own hosting and crawler policy; a file here
  would not change how GitHub is crawled and would only mislead.

## Discovery surfaces

Each surface below is either **controlled by this repository** (a file we author) or
**third-party** (hosted elsewhere). Third-party surfaces are listed so a reader knows where to look,
not as evidence of a result.

| Surface | What it is | Controlled by | How to verify |
| --- | --- | --- | --- |
| `README.md` first screen | Product identity, problem, differentiation, install | This repository | Read the first 40 lines of [README.md](../README.md) |
| `llms.txt` | Concise machine-readable summary | This repository | Fetch [`llms.txt`](../llms.txt) |
| `plugin.yaml` | Machine-readable plugin manifest | This repository | Read [plugin.yaml](../plugin.yaml) |
| `pyproject.toml` | Package name, version, dependencies | This repository | Read [pyproject.toml](../pyproject.toml) |
| Repository description | The one-line summary on the GitHub repo page | GitHub metadata | `gh repo view itzebe/hermes-assumption-ledger` |
| Repository topics | The topic tags on the GitHub repo page | GitHub metadata | `gh repo view itzebe/hermes-assumption-ledger --json repositoryTopics` |
| Hermes catalog entry | The listing Hermes users see | `NousResearch/hermes-agent` | `hermes plugins info assumption-ledger` |
| `LICENSE` | MIT license text | This repository | Read [LICENSE](../LICENSE) |

## Machine-readable entry point: `llms.txt`

`llms.txt` at the repository root is the single file an assistant or research tool should read
first. It contains product identity, the problem solved, technical differentiation, an architecture
overview, installation, tools and hooks, the verification lifecycle, security boundaries,
configuration, compatibility, testing evidence, known limitations and links to the primary docs.

It follows the emerging `llms.txt` convention: a plain-Markdown file at the repository root with an
`# H1` name, a `> ` blockquote summary, and prose sections. It is deliberately short — a summary,
not a mirror of the docs.

## Verifying the claims

Every factual claim in `llms.txt` is checkable from the source:

| Claim | Verify with |
| --- | --- |
| Six tools, four hooks | `hermes plugins doctor .` — or `grep -c 'register_tool' __init__.py` |
| Twelve check kinds | `grep -nE 'def check_' ledger/verifiers.py` (twelve functions), or the `supported_check_kinds` list in `assumption_status` |
| Zero LLM calls | `grep -rniE '\b(openai|anthropic|completion|chat_completion)\b' ledger/*.py __init__.py` returns no match |
| No runtime dependencies | `pyproject.toml` `dependencies = []`; `hermes plugins validate .` reports "none declared" |
| Version 0.2.2 everywhere | `python -m pytest tests/test_config.py::test_versions_agree_everywhere -q` |
| Catalog pin | `hermes plugins info assumption-ledger` prints the pinned SHA |
| Test count | `python -m pytest -q` |
| Non-vacuity | `python3 scripts/nonvacuous_check.py` |

## Repository metadata

The GitHub repository description and topics are set to match the source:

- Description: "A persistent, evidence-driven assumption ledger for Hermes Agent. Tracks operational
  assumptions, verifies them deterministically, preserves provenance, and blocks dependent decisions
  when evidence contradicts them."
- Topics: `ai-agents`, `ai-security`, `hermes-agent`, `hermes-agent-plugin`, `open-source`,
  `assumption-verification`, `deterministic-verification`, `plugin-catalog`.

Topics are descriptive, not aspirational: each names something the repository actually is.

## What is deliberately not done

- No fabricated badges. There is no CI badge because there is no CI (see
  [CHANGELOG.md](../CHANGELOG.md#ki-5-no-continuous-integration)).
- No invented adoption or usage statistics.
- No claim of guaranteed LLM indexing.
- No `robots.txt` pretending to control GitHub's crawler policy.
- No keyword stuffing: the same accurate terms are used everywhere, and nothing is asserted that the
  source does not support.

## Keeping this current

When the product changes, update, in order: the source, `plugin.yaml`, `README.md`'s first screen,
`llms.txt`, and this document. The `llms.txt` summary and the README's opening paragraph must stay
consistent; the version and the tool/hook counts appear in both and must agree with the code.
