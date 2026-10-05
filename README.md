# durable-claude-agent

A Claude-powered text-to-SQL analyst over a governed lakehouse, run as a **Temporal workflow**
so every step gets retries, timeouts, a human-approval gate before risky queries, and crash
recovery. Ships with an evaluation harness that scores SQL correctness, tool use, and
retrieval quality.

> **Status: M0 (scaffold).** Milestones:
>
> | # | Milestone | State |
> |---|-----------|-------|
> | M0 | Repo scaffold: uv, ruff, mypy, pytest, pre-commit + secrets scan, CI | ✅ |
> | M1 | Seed data (Delta Lake via delta-rs) + DuckDB + deterministic `risk.py` | ⏳ |
> | M2 | MCP server (4 tools) + LanceDB retrieval over `governance/` | ⏳ |
> | M3 | Temporal workflow, approval signal, CLI | ⏳ |
> | M4 | Crash-recovery demo + recording | ⏳ |
> | M5 | Eval harness + baseline scorecard | ⏳ |
> | M6 | Observability (OTel → Phoenix), docs, CI evals | ⏳ |

## Quickstart (so far)

```bash
make install      # uv sync + pre-commit hooks
make check        # ruff, mypy --strict, pytest, detect-secrets
```

Copy `.env.example` to `.env` and fill in `ANTHROPIC_API_KEY` when you reach M3.
No secrets are ever committed; `detect-secrets` runs on every commit and in CI.
