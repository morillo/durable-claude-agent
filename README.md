# durable-claude-agent

A Claude-powered text-to-SQL analyst over a governed lakehouse, run as a **Temporal workflow**
so every step gets retries, timeouts, a human-approval gate before risky queries, and crash
recovery. Ships with an evaluation harness that scores SQL correctness, tool use, and
retrieval quality.

> **Status: M2 complete.** Milestones:
>
> | # | Milestone | State |
> |---|-----------|-------|
> | M0 | Repo scaffold: uv, ruff, mypy, pytest, pre-commit + secrets scan, CI | ✅ |
> | M1 | Seed data (Delta Lake via delta-rs) + DuckDB + deterministic `risk.py` | ✅ |
> | M2 | MCP server (4 tools) + LanceDB retrieval over `governance/` | ✅ |
> | M3 | Temporal workflow, approval signal, CLI | ⏳ |
> | M4 | Crash-recovery demo + recording | ⏳ |
> | M5 | Eval harness + baseline scorecard | ⏳ |
> | M6 | Observability (OTel → Phoenix), docs, CI evals | ⏳ |

## Quickstart (so far)

```bash
make install      # uv sync + pre-commit hooks
make check        # ruff, mypy --strict, pytest, detect-secrets
make seed         # ~11k rows of synthetic retail data as Delta tables in data/lakehouse/
make index        # LanceDB index over governance/*.md (downloads MiniLM once, ~90 MB)
make mcp          # MCP tool server on http://127.0.0.1:8765/mcp
```

## The governed lakehouse (M1)

Five Delta Lake tables written with [delta-rs](https://delta-io.github.io/delta-rs/) and read
by DuckDB's `delta` extension through views of the same name: `customers`, `products`,
`orders` (partitioned by `order_year`), `order_items`, and the **restricted** `payment_cards`.
The dataset is deterministic (`seed=42`) and contains deliberate governance traps:
cancelled orders keep a `gross_amount`, refunds live in their own column, and shipping is not
revenue. `governance/data_dictionary.md` defines the metrics; `governance/policies.md` defines
PII columns, restricted tables, and query-shape rules, and embeds a TOML block that
`risk.py` loads so the document is the single source of truth.

`risk.py` classifies every SQL string as `safe`, `needs_approval`, or `blocked` by parsing it
with sqlglot, resolving columns against the live schema (so `SELECT *` on `customers` is seen
to touch PII), and applying the policy rules mechanically. The model's own risk flag is
recorded but never trusted for control flow.

Copy `.env.example` to `.env` and fill in `ANTHROPIC_API_KEY` when you reach M3.
No secrets are ever committed; `detect-secrets` runs on every commit and in CI.

## Tools and retrieval (M2)

One MCP server (official `mcp` Python SDK, Streamable HTTP) exposes four tools:

| Tool | Who may call it | What it does |
|---|---|---|
| `get_schema` | model | DDL for queryable tables with PII sample values redacted and restricted tables omitted |
| `search_governance` | model | top-k passages from `governance/*.md` with `file#anchor` citations |
| `validate_sql` | model | dry run: DuckDB `EXPLAIN` plus the deterministic risk class and the policy sections it cites |
| `run_sql` | orchestrator only | executes with row cap and timeout; refuses callers without the capability token and refuses `blocked` SQL even with it |

**Why tool-side enforcement.** A system prompt saying "never call `run_sql`" is a request, not
a control. The `generate_sql` step filters `run_sql` out of the tool list Claude sees, *and*
`run_sql` checks an `Authorization: Bearer` header in constant time against a secret only the
Temporal worker holds. Two independent controls must fail before the model can execute
anything, and even then the policy classifier runs again inside the tool.

**Retrieval** is local and costs zero tokens: `governance/*.md` is chunked by heading, embedded
with `all-MiniLM-L6-v2` via sentence-transformers, and stored in LanceDB under `data/index/`.
Each chunk keeps its heading path (for example `Query shape rules > Row limits`), the same
identifier the risk classifier cites, so a final answer can cite a section whether it came from
retrieval or from policy enforcement. LanceDB was chosen over ChromaDB because it is embedded
and Arrow-native, lives in a directory next to the Delta tables, and needs no server process
for a corpus of a few dozen chunks.
