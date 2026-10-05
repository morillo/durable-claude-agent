# Architecture Decision Records

One paragraph each: the decision, the alternatives considered, and why. Newest last.

| ADR | Decision |
|---|---|
| [001](001-temporal-for-orchestration.md) | Temporal workflows for the agent loop's durability |
| [002](002-mcp-over-http-with-capability-token.md) | MCP tools over Streamable HTTP; `run_sql` gated by a bearer capability |
| [003](003-lancedb-for-retrieval.md) | LanceDB + local MiniLM embeddings for governance retrieval |
| [004](004-deterministic-risk-classifier.md) | sqlglot-based risk classification as the only gate signal |
| [005](005-duckdb-delta-local-lakehouse.md) | DuckDB over Delta Lake as the zero-infra lakehouse |
| [006](006-model-routing.md) | Haiku for plan/summary, Sonnet for SQL and judging, effort as the dial |
| [007](007-heartbeat-checkpointing.md) | Checkpoint the tool loop in heartbeat details |
| [008](008-response-cache.md) | Request-hash response cache for idempotent retries and cheap evals |
| [009](009-manual-tool-loop.md) | Manual Messages API tool loop instead of the SDK tool runner |
| [010](010-evals-through-the-workflow.md) | Evals run the real workflow on a private dev server |
| [011](011-secrets-scanning.md) | detect-secrets in pre-commit and CI |
