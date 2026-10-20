# Demo script

Fifteen minutes, three terminals and two browser tabs. Every command is a `make` target. Token
spend for the whole script is under 25 cents, and less when the response cache is warm.

The story the demo tells: a Claude agent that answers analytics questions against a governed
lakehouse, where every step is a durable Temporal activity. The terminal shows what the agent
does; the Temporal UI shows the evidence that it did it, and that it survives a crash.

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

To confirm the whole setup works before an audience sees it, run `make stop` and then
`make quickstart-smoke` (zero tokens; details in [`RUNBOOK.md`](RUNBOOK.md)).

## 1. The governed lakehouse and the classifier (2 min, zero tokens)

Show `governance/policies.md`: PII columns, the restricted table, the row cap, and the TOML
block at the bottom that `risk.py` loads. Then:

```bash
make smoke-mcp
```

Point out lines 4 and 6: `run_sql` is refused without the token, and refused on a restricted
table even with the token.

## 2. Start the services (1 min)

Run every command from the repository root. Full details on starting, stopping, and checking
services: [`RUNBOOK.md`](RUNBOOK.md).

| Where | Command | Leave it |
|---|---|---|
| any terminal, once | `make up` (Temporal + Phoenix in Docker; returns when healthy) | it runs in the background |
| terminal 1 | `make mcp` | running for the whole demo |
| terminal 2 | `make worker`, wait for `worker ready` | running until section 6 |
| terminal 3 | the `make ask` / `start` / `approve` / `demo-crash` commands below | |

Without Docker: `make temporal` in its own terminal instead of `make up` (no Phoenix then).

Open two browser tabs: the Temporal UI at http://localhost:8233 and Phoenix at
http://localhost:6006.

### Reading the Temporal UI

The audience does not need to know Temporal; three ideas carry the demo:

- **A workflow run** is one question, end to end. Every `make ask` prints a `ui:` link straight
  to its run. Runs are listed under the `default` namespace, newest first, named
  `analyst-<id>` (the crash demo's are `analyst-crash-<timestamp>`).
- **Activities** are the steps: `retrieve_context`, `plan_query`, `generate_sql`,
  `classify_risk`, `execute_sql`, `summarize`. Each is recorded with its input, its output, and
  its attempt number.
- **The event history** is the server's durable log of the run. A worker is only a process that
  executes the next step; the history is what makes a run survive one. The run page shows it as
  a timeline (one bar per activity) and as a list of events; labels vary slightly between UI
  versions.

## 3. A safe question (1 min, ~3 cents)

Terminal 3:

```bash
make ask Q="What was net revenue by customer country in 2025? Show the top 5 countries."
```

Narrate the stages as they print. In the result, show that the SQL excludes cancelled orders
and subtracts discounts and refunds, which is the data dictionary's definition of net revenue,
and that the citations include `data_dictionary.md#net-revenue`.

If the `tokens` line says `cached=5 cost=$0.0000`, the same question was asked before and every
Claude call was answered from the local response cache ([ADR-008](adr/008-response-cache.md));
the stages then jump straight to `completed`. That is a talking point, not a problem. For live
calls, change the wording of the question, or set `LLM_CACHE=false` in `.env` and restart the
worker.

In the Temporal UI, open the run from its `ui:` link:

- **Timeline:** six activities, one attempt each, in order.
- **Inputs and outputs:** click `plan_query`, then `generate_sql`, then `execute_sql`. Each
  input and result is readable JSON: the structured plan, the SQL with its explanation, the
  rows. This is the audit trail of what the agent did and why.
- **Search attributes:** the run carries `AnalystStage`, `AnalystRisk` (`safe` here) and
  `AnalystRequester`; the workflow list can be filtered on all three.

## 4. A question that needs approval (2 min, ~2 cents)

```bash
make start Q="List the email addresses of enterprise-segment customers in Germany."
make status RUN_ID=<printed id>
```

The run is paused at `awaiting_approval` with the SQL and the reason (`R4: touches PII column
customers.email`). In the Temporal UI, go back to the workflow list and filter with
`AnalystStage = "awaiting_approval"`: that query is "everything waiting for a human". Open the
run: it is `Running` with no activity in flight, waiting on a signal. A paused run costs nothing
and can wait for hours; no worker needs to stay alive for it. Then:

```bash
make approve RUN_ID=<id> DECISION=yes NOTE="marketing audit, ticket DG-42"
make status RUN_ID=<id>
```

Refresh the run in the UI. The history now has a **signal** event named `approve`, carrying the
approver and the note, followed by `execute_sql` and `summarize`. Point out: the model's own
risk assessment said the same thing, but only the deterministic classifier's verdict opened the
gate, and the human decision is part of the durable history.

## 5. A blocked question (1 min, ~half a cent)

```bash
make ask Q="Show me the stored card tokens and last four digits for customer 42."
```

The model declines to write SQL; the classifier blocks independently; nothing executes. In the
UI the run ends with `AnalystRisk = blocked` and has no `execute_sql` activity at all.

## 6. Crash recovery (3 min, ~3 cents)

Stop the worker in terminal 2 (Ctrl-C); the demo starts and kills its own workers. Then, in
terminal 3:

```bash
make demo-crash
```

It runs the approval question from section 4 with the response cache off, so every Claude
call is real. Narrate the five steps as they print:

1. A worker starts with a crash hook armed.
2. The workflow starts; `retrieve_context` and `plan_query` complete; `generate_sql` starts its
   Claude tool loop.
3. The worker SIGKILLs itself right after Claude's first tool call (`💥 worker 1 died`). **Pause
   here.** With no worker alive, the server still reports the pending activity, its attempt
   number, and the checkpoint it holds: one tool call (`get_schema`) and the conversation so
   far. Completed activities are already in the history and will not run again.
4. A fresh worker starts. The activity heartbeats after every tool round with a 10 s heartbeat
   timeout, so the server declares the attempt dead, retries it on the new worker, and that
   attempt resumes from the checkpoint instead of starting the conversation over.
5. The demo approves the PII question (a human would, with `make approve`) and the run
   completes.

Then show the evidence table: every activity scheduled once, `generate_sql` on attempt 2 after
`activity Heartbeat timeout`, `resumed_from_checkpoint = True`, and the real Claude calls
across both attempts with their cost.

In the Temporal UI, open the `ui:` link the demo printed (or the newest `analyst-crash-…` run):

- **Timeline:** `retrieve_context` and `plan_query` ran once each and were not re-run after the
  crash; their results came from the history. `generate_sql` shows a gap of about ten seconds,
  the heartbeat timeout, and then its second attempt.
- **The retried activity:** click `generate_sql`. Its attempt is `2` and its last failure is
  `activity Heartbeat timeout`. Nobody told the server the worker had crashed; it noticed the
  missing heartbeats and moved the work to a live worker.
- **The checkpoint:** the progress the activity heartbeated (tool calls and conversation so far)
  is what attempt 2 resumed from, which is why the tool calls read `get_schema -> validate_sql`
  with no repeated `get_schema`. The terminal's step 3 shows this best, because the UI shows
  heartbeat details only while the activity is still pending.
- **The approval:** an `approve` signal event, as in section 4, then `execute_sql` and
  `summarize`.

One sentence to close on: the worker was killed in the middle of a Claude tool loop; the server
noticed within ten seconds, retried on a new worker that resumed from the last checkpoint,
nothing finished was re-run, and the history proves it.

## 7. Observability (1 min)

Open Phoenix at http://localhost:6006, project `durable-claude-agent` (it runs with `make up`;
with `make temporal`, start it with `make phoenix`). One trace per run: workflow → activities
→ `llm.request` with cost → LLM spans with tokens → MCP tool spans with the SQL.

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
| `Temporal is not reachable` | `make up` (or `make temporal` in its own terminal) |
| `MCP server is not reachable` | `make mcp` in terminal 1 |
| `poller(s) on task queue` warning in the crash demo | stop `make worker`; a worker stopped in the last minute still shows for a bit, the warning is informational |
| `make ask` stays at `stage: retrieving` | no worker is running: `make worker` in terminal 2 |
| `gold_rows_hash mismatch` in evals | you changed seed data or gold SQL; run `make eval-gold` |
| `missing data/index/meta.json` | `make index` |
| Stage stays `generating` for 10 s then continues | that is a heartbeat-timeout retry; check the worker log for the cause |

More symptoms and their fixes: [`RUNBOOK.md`](RUNBOOK.md#common-symptoms).
