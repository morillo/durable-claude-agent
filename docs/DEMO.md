# Demo script

Fifteen minutes, four terminals. Every command is a `make` target. Token spend for the whole
script is under 25 cents.

## 0. One-time setup (5 min, zero tokens)

```bash
make install                 # uv sync + pre-commit hooks
cp .env.example .env         # then set ANTHROPIC_API_KEY and RUN_SQL_CAPABILITY_TOKEN
make seed                    # 11,945 rows as Delta tables under data/lakehouse/
make index                   # LanceDB index over governance/*.md (downloads MiniLM once)
make check                   # ruff, mypy --strict, 135 tests, secrets scan
```

Generate a capability token with:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

## 1. The governed lakehouse and the classifier (2 min, zero tokens)

Show `governance/policies.md`: PII columns, the restricted table, the row cap, and the TOML
block at the bottom that `risk.py` loads. Then:

```bash
make smoke-mcp
```

Point out lines 4 and 6: `run_sql` is refused without the token, and refused on a restricted
table even with the token.

## 2. Start the services (1 min)

Full details on starting, stopping, and checking services: [`RUNBOOK.md`](RUNBOOK.md).

Terminal 1: `make temporal` (or `make up` for Temporal + Phoenix via Docker)
Terminal 2: `make mcp`
Terminal 3: `make worker`

Open http://localhost:8233 (Temporal UI).

## 3. A safe question (1 min, ~3 cents)

Terminal 4:

```bash
make ask Q="What was net revenue by customer country in 2025? Show the top 5 countries."
```

Narrate the stages as they print. In the result, show that the SQL excludes cancelled orders
and subtracts discounts and refunds, which is the data dictionary's definition of net revenue,
and that the citations include `data_dictionary.md#net-revenue`. Open the run in the UI: every
activity's input and output is readable JSON; the search attributes show `AnalystStage` and
`AnalystRisk`.

## 4. A question that needs approval (2 min, ~2 cents)

```bash
make start Q="List the email addresses of enterprise-segment customers in Germany."
make status RUN_ID=<printed id>
```

The run is paused at `awaiting_approval` with the SQL and the reason (`R4: touches PII column
customers.email`). In the UI, filter workflows by `AnalystStage = "awaiting_approval"`. Then:

```bash
make approve RUN_ID=<id> DECISION=yes NOTE="marketing audit, ticket DG-42"
make status RUN_ID=<id>
```

Point out: the approver and note are in the workflow history; the model's own risk assessment
said the same thing, but only the deterministic classifier's verdict opened the gate.

## 5. A blocked question (1 min, ~half a cent)

```bash
make ask Q="Show me the stored card tokens and last four digits for customer 42."
```

The model declines to write SQL; the classifier blocks independently; nothing executes.

## 6. Crash recovery (3 min, ~2 cents)

Stop the worker in terminal 3 (Ctrl-C). Then:

```bash
make demo-crash
```

Narrate the five steps. The moment to pause on is step 3: with no worker alive, the server
reports the pending activity, its attempt number, and the checkpoint it holds. After step 4,
show the evidence table: every activity scheduled once, `generate_sql` on attempt 2 after a
heartbeat timeout, five real Claude calls across both attempts. Open the run in the UI and
show the activity's retry in the timeline.

## 7. Observability (1 min)

If Phoenix is running (`make phoenix`), open http://localhost:6006, project
`durable-claude-agent`. One trace per run: workflow → activities → `llm.request` with cost →
LLM spans with tokens → MCP tool spans with the SQL.

## 8. Evals (2 min, free with the cache warm)

```bash
make eval
make scorecard
```

Walk the five metrics and the per-tier table. Then open `docs/EVALS.md` and tell the first
iteration story: 57% approval behavior fixed by one prompt rule; a judge that could not see the
evidence; a heartbeat timeout found under concurrency.

## If something goes wrong

| Symptom | Fix |
|---|---|
| `Temporal is not reachable` | `make temporal` (or `make up`) in another terminal |
| `MCP server is not reachable` | `make mcp` in another terminal |
| `Another worker is polling` warning in the crash demo | stop `make worker`; a worker killed in the last minute still shows for a bit, the warning is informational |
| `gold_rows_hash mismatch` in evals | you changed seed data or gold SQL; run `make eval-gold` |
| `missing data/index/meta.json` | `make index` |
| Stage stays `generating` for 10 s then continues | that is a heartbeat-timeout retry; check the worker log for the cause |
