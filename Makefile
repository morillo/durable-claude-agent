# durable-claude-agent — developer entrypoints.
# Every target is safe to re-run. Targets that spend API tokens say so.

UV ?= uv
PY  = $(UV) run

.DEFAULT_GOAL := help

.PHONY: help install lint format typecheck test check secrets-scan seed index mcp

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
