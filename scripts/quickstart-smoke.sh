#!/usr/bin/env bash
# Run the README Quickstart the way a fresh clone does, and fail loudly on the first broken step.
#
#   make quickstart-smoke           # zero tokens: seed, index, make up, make mcp, make worker
#   make quickstart-smoke FULL=1    # also make ask and make demo-crash (~5 cents, needs a real key)
#   make quickstart-smoke ROUTE=brew  # Temporal via `make temporal` (brew CLI) instead of `make up`
#
# Docker services run as a separate compose project (dca-smoke) on brand-new volumes, exactly
# like a first `make up`; the brew route uses a throwaway SQLite file under data/quickstart-smoke/.
# Everything it starts is stopped and its volumes removed on exit. Your own compose project and
# data/temporal.sqlite are not touched. CI runs the docker route on every PR and push.
set -euo pipefail
cd "$(dirname "$0")/.."

export COMPOSE_PROJECT_NAME=dca-smoke
LOG_DIR=data/quickstart-smoke
QUEUE=analyst
Q="What was net revenue by customer country in 2025? Top 5."
FULL="${FULL:-0}"
ROUTE="${ROUTE:-docker}"
pids=()

step() { printf '\n==> %s\n' "$*"; }
fail() { printf '\nQUICKSTART SMOKE FAILED: %s\n' "$*" >&2; exit 1; }
port_open() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

cleanup() {
  status=$?
  set +e
  step "cleanup"
  for pid in "${pids[@]}"; do kill -- "-$pid" 2>/dev/null; { wait "$pid"; } 2>/dev/null; done
  if [ "$status" -ne 0 ]; then
    for f in "$LOG_DIR"/*.log; do [ -f "$f" ] && { echo "--- $f (last 40 lines)"; tail -40 "$f"; }; done
    [ "$ROUTE" = "docker" ] && { echo "--- docker compose logs temporal (last 40 lines)"; docker compose logs --tail 40 temporal; }
  fi
  docker compose down -v --remove-orphans >/dev/null 2>&1
  [ "$status" -eq 0 ] && printf '\nQUICKSTART SMOKE PASSED\n'
}

# Background services get their own process group (set -m) so cleanup stops uv and its child.
start_bg() {
  local name=$1; shift
  set -m; "$@" >"$LOG_DIR/$name.log" 2>&1 & pids+=($!); set +m
}

wait_for() {  # wait_for <seconds> <description> <command...>
  local secs=$1 what=$2; shift 2
  for _ in $(seq "$secs"); do "$@" >/dev/null 2>&1 && return 0; sleep 1; done
  fail "$what did not happen within ${secs}s"
}

step "preflight: ports 7233, 8233, 8765 and 6006 must be free, no worker running"
for p in 7233 8233 8765 6006; do
  port_open "$p" && fail "port $p is in use. Run \`make stop\` first (your data is kept), then retry"
done
pgrep -f "python -m durable_agent.worker$" >/dev/null && fail "a worker is running; stop it first"
trap cleanup EXIT
mkdir -p "$LOG_DIR"

step "cp .env.example .env (only if .env is missing)"
if [ ! -f .env ]; then
  cp .env.example .env
  token=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
  sed -i.bak "s|^RUN_SQL_CAPABILITY_TOKEN=.*|RUN_SQL_CAPABILITY_TOKEN=$token|" .env && rm -f .env.bak
fi

step "make seed && make index"
make seed
make index

if [ "$ROUTE" = "brew" ]; then
  step "make temporal (background, fresh db) + make phoenix"
  rm -f "$LOG_DIR"/temporal.sqlite*
  start_bg temporal make temporal TEMPORAL_DB="$LOG_DIR/temporal.sqlite"
  make phoenix
  wait_for 60 "Temporal answering on :7233" temporal operator cluster health --address localhost:7233
  describe_queue() { temporal task-queue describe --task-queue "$QUEUE" --address localhost:7233; }
else
  step "make up (fresh volumes, images pulled first so the test sees what a fresh clone gets)"
  docker compose pull -q
  make up
  describe_queue() {
    docker compose exec -T temporal temporal task-queue describe --task-queue "$QUEUE" --address localhost:7233
  }
fi

step "make mcp (background, log in $LOG_DIR/mcp.log)"
start_bg mcp make mcp
wait_for 60 "MCP server listening on :8765" port_open 8765

step "make worker (background, log in $LOG_DIR/worker.log)"
start_bg worker make worker
wait_for 60 "worker ready log line" grep -q "worker ready" "$LOG_DIR/worker.log"
worker_polling() { describe_queue | grep -q '@'; }
wait_for 30 "worker polling task queue '$QUEUE'" worker_polling
echo "worker is connected to Temporal and polling '$QUEUE'"

if [ "$FULL" != "1" ]; then
  echo "(skipping make ask and make demo-crash: run with FULL=1 to spend ~5 cents on them)"
  exit 0
fi

step "make ask"
make ask Q="$Q" | tee "$LOG_DIR/ask.log"
grep -q "== COMPLETED ==" "$LOG_DIR/ask.log" || fail "make ask did not complete"

step "stop the worker, then make demo-crash"
worker_pid=${pids[${#pids[@]}-1]}
kill -- "-$worker_pid"; { wait "$worker_pid"; } 2>/dev/null || true
make demo-crash
