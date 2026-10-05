# durable-claude-agent — developer entrypoints.
# Every target is safe to re-run. Targets that spend API tokens say so.

UV ?= uv
PY  = $(UV) run

.DEFAULT_GOAL := help

.PHONY: help install lint format typecheck test check secrets-scan seed index mcp smoke-mcp temporal worker ask start approve status

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install: ## Create the virtualenv and install all dependency groups
	$(UV) sync --all-groups
	$(PY) pre-commit install

lint: ## Ruff lint + format check
	$(PY) ruff check .
	$(PY) ruff format --check .

format: ## Auto-fix lint and formatting
	$(PY) ruff check --fix .
	$(PY) ruff format .

typecheck: ## mypy --strict over src/
	$(PY) mypy

test: ## Unit tests (no API calls, no Temporal server)
	$(PY) pytest

secrets-scan: ## Scan the tree for committed secrets against the baseline
	$(PY) detect-secrets scan --baseline .secrets.baseline

check: lint typecheck test secrets-scan ## Everything CI runs

# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
LAKEHOUSE_PATH ?= data/lakehouse

seed: ## Generate the synthetic retail Delta tables under data/lakehouse (zero tokens)
	$(PY) python -m durable_agent.seed --out $(LAKEHOUSE_PATH)

INDEX_PATH ?= data/index

index: ## Build the LanceDB index over governance/*.md with a local MiniLM model (zero tokens)
	$(PY) python -m durable_agent.retrieval.index --governance governance --out $(INDEX_PATH)

# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------

mcp: ## Run the MCP tool server over Streamable HTTP (reads .env for MCP_SERVER_URL and the capability token)
	$(PY) python -m durable_agent.mcp_server

smoke-mcp: ## Start the MCP server, exercise all four tools, stop it. One command, zero tokens.
	$(PY) python -m durable_agent.mcp_server.smoke

TEMPORAL_DB ?= data/temporal.sqlite
# Custom search attributes used by the workflow; they must exist before a run upserts them.
SEARCH_ATTRS = --search-attribute AnalystStage=Keyword --search-attribute AnalystRisk=Keyword --search-attribute AnalystRequester=Keyword

temporal: ## Run the Temporal dev server (UI on http://localhost:8233), state persisted in data/temporal.sqlite
	temporal server start-dev --db-filename $(TEMPORAL_DB) $(SEARCH_ATTRS)

worker: ## Run the Temporal worker (workflow + activities). Needs temporal and mcp running.
	$(PY) python -m durable_agent.worker

# ---------------------------------------------------------------------------
# Using the agent (each `ask` spends a few cents of Anthropic tokens)
# ---------------------------------------------------------------------------
REQUESTER ?= $(USER)

ask: ## Ask a question and wait for the answer:  make ask Q="net revenue by country in 2025"
	@test -n "$(Q)" || (echo 'usage: make ask Q="your question"'; exit 1)
	$(PY) durable-agent ask "$(Q)" --requester $(REQUESTER)

start: ## Start a run without waiting:  make start Q="..."
	@test -n "$(Q)" || (echo 'usage: make start Q="your question"'; exit 1)
	$(PY) durable-agent start "$(Q)" --requester $(REQUESTER)

approve: ## Decide a run awaiting approval:  make approve RUN_ID=analyst-abc123 DECISION=yes NOTE="ticket 42"
	@test -n "$(RUN_ID)" -a -n "$(DECISION)" || (echo 'usage: make approve RUN_ID=... DECISION=yes|no [NOTE="..."]'; exit 1)
	$(PY) durable-agent approve $(RUN_ID) $(DECISION) --note "$(NOTE)" --by $(REQUESTER)

status: ## Show the live state of a run:  make status RUN_ID=analyst-abc123
	@test -n "$(RUN_ID)" || (echo 'usage: make status RUN_ID=...'; exit 1)
	$(PY) durable-agent status $(RUN_ID)
