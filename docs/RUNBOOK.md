# Runbook: local services

Everything runs on your machine. Four services exist; most commands need none of them. All
commands run from the repository root.

## See what is running

```bash
make ps
```

Prints each project process with its pid, which process owns each port, and the Docker
services. Two pids per Python service is normal (the `uv run` wrapper and its `python` child).

| Port | Service |
|---|---|
| 7233 | Temporal gRPC frontend |
| 8233 | Temporal Web UI (http://localhost:8233) |
| 8765 | MCP tool server (http://127.0.0.1:8765/mcp) |
| 6006 | Phoenix UI and OTLP receiver (http://localhost:6006) |

## Stop everything

```bash
make stop
```

Kills the worker, the MCP server, and the Temporal dev server, runs `docker compose down`,
then prints `make ps` so you can confirm nothing is left. Safe to run when nothing is running.

## Start

Each service blocks its terminal and logs there: one terminal per service.

| Terminal | Command | What it is | Stop with |
|---|---|---|---|
| 1 | `make temporal` | Temporal dev server (brew-installed CLI), state in `data/temporal.sqlite` | Ctrl-C |
| 2 | `make mcp` | MCP tool server | Ctrl-C |
| 3 | `make worker` | Temporal worker running the workflow and activities | Ctrl-C |
| 4 | `make ask Q="..."` | a question | finishes on its own |

Alternative for terminal 1: `make up` starts Temporal **and** Phoenix in Docker and returns
once Temporal passes its health check (it fails after 90 s if Temporal never comes up; see
`docker compose logs temporal`); `make down` stops them. Do not combine `make temporal` with `make up`: both want
port 7233. Phoenix alone: `make phoenix`.

Prerequisites for the Temporal CLI route: `brew install temporal`. For the Docker route: Docker
Desktop running.

## Check that the whole setup works

```bash
make stop                          # the smoke test needs ports 7233, 8233, 8765, 6006 free
make quickstart-smoke              # zero tokens
make quickstart-smoke FULL=1       # also make ask + make demo-crash, about 5 cents
make quickstart-smoke ROUTE=brew   # Temporal via `make temporal` instead of `make up`
```

Runs the README Quickstart from fresh Docker volumes (a separate compose project, `dca-smoke`)
and checks that the worker is polling Temporal. Your own volumes, `data/temporal.sqlite` and
`.env` are left alone; if `.env` is missing it creates one from `.env.example`. Prints
`QUICKSTART SMOKE PASSED`, or the failing step plus the last lines of each service log. Service
logs stay in `data/quickstart-smoke/`. CI runs the docker route on every pull request and push to
`main`; a manual CI dispatch with `run_quickstart_full` runs `FULL=1`.

## Which services does each command need?

| Command | Needs running |
|---|---|
| `make check`, `make test`, `make eval`, `make smoke-mcp` | nothing: they start private servers and stop them |
| `make seed`, `make index` | nothing |
| `make ask`, `make start`, `make approve`, `make status` | Temporal, MCP server, worker |
| `make demo-crash` | Temporal and MCP server only. Stop `make worker` first; the demo starts and kills its own workers |
| `make record-demo` | same as `demo-crash`, plus `brew install vhs` |
| `make quickstart-smoke` | nothing, and ports 7233, 8233, 8765, 6006 free: it starts and stops its own services |
| Phoenix traces | Phoenix (`make phoenix` or `make up`) and `OTEL_ENABLED=true` in `.env` (the default) |

## Configuration

Copy `.env.example` to `.env`. Required for anything that calls Claude: `ANTHROPIC_API_KEY`.
Required for `make ask` to execute SQL: `RUN_SQL_CAPABILITY_TOKEN` (any long random string;
generate one with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`). The worker
and the MCP server must see the same value, which they do when both read the same `.env`.

Model routing, effort, cache, ports, and paths are all in `.env.example` with comments.

## If a port is already taken

`make ps` shows the owner. If it is a process you do not recognize:

```bash
lsof -nP -iTCP:7233 -sTCP:LISTEN     # replace with the port in question
kill <pid>
```

## What survives a stop

| Data | Location | Reset with |
|---|---|---|
| Temporal runs and history (`make temporal`) | `data/temporal.sqlite` | delete the file (while Temporal is stopped) |
| Temporal runs and history (`make up`) | Docker volume `temporal-data` | `docker compose down -v` (also clears Phoenix) |
| LLM response cache | `data/llm_cache.sqlite` | delete the file, or set `LLM_CACHE=false` |
| Seeded lakehouse | `data/lakehouse/` | `make seed` (deterministic; same data every time) |
| Retrieval index | `data/index/` | `make index` |
| Phoenix traces | Docker volume `phoenix-data` | `docker compose down -v` |
| Eval results | `evals/results/<timestamp>/` | delete folders; `make scorecard` shows the latest |

Everything under `data/` and `evals/results/` is gitignored.

## Logs

Services log to the terminal they run in. The crash demo writes its two workers' logs to
`data/demo/worker-1.log` and `worker-2.log`. The Temporal UI (http://localhost:8233) shows every
run's inputs, outputs, retries, and the approval signal; it is usually the fastest way to see
what a run did.

## Common symptoms

| Symptom | Cause and fix |
|---|---|
| `Temporal is not reachable at localhost:7233` | start `make temporal` or `make up` |
| `make worker` fails with `Connection refused` on `127.0.0.1:7233` although `make up` succeeded | the Temporal container is crash-looping: `docker compose ps` shows `Restarting`. Read `docker compose logs temporal` |
| `unable to create SQLite admin DB: unable to open database file (14)` in `docker compose logs temporal` | the `temporal-data` volume is owned by root while Temporal runs as uid 1000. `make up` fixes this with its `temporal-init` step; on an older checkout, pull and run `docker compose down && make up` |
| `nc -z localhost 7233` succeeds but nothing connects | Docker Desktop's port proxy accepts connections even when the container is down; check `docker compose ps` instead |
| `MCP server is not reachable` | start `make mcp` |
| `make ask` prints `stage: retrieving` and never advances | no worker is running: start `make worker` |
| `run_sql requires the run_sql capability` in a worker log | `RUN_SQL_CAPABILITY_TOKEN` differs between the worker's and the MCP server's environment, or is unset |
| `Could not resolve authentication method` | `ANTHROPIC_API_KEY` missing from `.env` |
| `gold_rows_hash mismatch` from `make eval` | seed data or gold SQL changed; run `make eval-gold` |
| `missing data/index/meta.json` | `make index` |
| Stage sits at `generating` for about 10 s, then continues | a heartbeat-timeout retry; the worker log says why the first attempt stalled |
| Crash demo warns `poller(s) on task queue` | a worker stopped in the last minute still shows as a poller; informational unless `make worker` is really running |
