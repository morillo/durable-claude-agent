# durable-claude-agent

A Claude-powered text-to-SQL analyst over a governed lakehouse, run as a **Temporal workflow**
so every step gets retries, timeouts, a human-approval gate before risky queries, and crash
recovery. Ships with an evaluation harness that scores SQL correctness, tool use, and
retrieval quality.

> **Status: M1 complete.** Milestones:
>
> | # | Milestone | State |
> |---|-----------|-------|
> | M0 | Repo scaffold: uv, ruff, mypy, pytest, pre-commit + secrets scan, CI | ✅ |
> | M1 | Seed data (Delta Lake via delta-rs) + DuckDB + deterministic `risk.py` | ✅ |
> | M2 | MCP server (4 tools) + LanceDB retrieval over `governance/` | ⏳ |
> | M3 | Temporal workflow, approval signal, CLI | ⏳ |
> | M4 | Crash-recovery demo + recording | ⏳ |
> | M5 | Eval harness + baseline scorecard | ⏳ |
> | M6 | Observability (OTel → Phoenix), docs, CI evals | ⏳ |

## Quickstart (so far)

```bash
make install      # uv sync + pre-commit hooks
make check        # ruff, mypy --strict, pytest, detect-secrets
make seed         # ~11k rows of synthetic retail data as Delta tables in data/lakehouse/
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
