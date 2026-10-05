"""MCP server tests: in-memory for tool semantics, real HTTP for the capability header."""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from mcp.client import Client

from durable_agent.config import Settings
from durable_agent.mcp_server import MODEL_TOOLS, ToolClient, create_server, tool_client
from durable_agent.mcp_server.client import ToolCallError
from durable_agent.mcp_server.server import AppState
from durable_agent.retrieval import HashEmbedder, build_index
from tests.conftest import GOVERNANCE_DIR

TOKEN = "test-capability-token"


@pytest.fixture(scope="module")
def state(tmp_path_factory: pytest.TempPathFactory, lakehouse_path: Path) -> AppState:
    index_path = tmp_path_factory.mktemp("index")
    embedder = HashEmbedder()
    build_index(GOVERNANCE_DIR, index_path, embedder)
    settings = Settings(
        _env_file=None,
        lakehouse_path=lakehouse_path,
        index_path=index_path,
        governance_path=GOVERNANCE_DIR,
        run_sql_capability_token=TOKEN,
        max_rows=50,
        query_timeout_seconds=5,
    )
    return AppState.from_settings(settings, embedder=embedder)


def mem_client(state: AppState) -> ToolClient:
    """In-memory connection: no transport headers, so run_sql must refuse.

    Opened inside each test (not as an async fixture) because anyio cancel scopes must be
    entered and exited in the same task.
    """
    return ToolClient(Client(create_server(state)))


# ---------------------------------------------------------------------------------------------
# Tool semantics (in-memory)
# ---------------------------------------------------------------------------------------------


async def test_tool_catalogue(state: AppState) -> None:
    async with mem_client(state) as mem:
        names = await mem.tool_names()
        assert set(names) == {*MODEL_TOOLS, "run_sql"}
        tools = (await mem.raw.list_tools()).tools
        by_name = {t.name: t for t in tools}
        assert by_name["get_schema"].annotations is not None
        assert by_name["get_schema"].annotations.read_only_hint is True
        assert by_name["run_sql"].meta == {"requires_capability": "run_sql"}
        assert by_name["validate_sql"].output_schema is not None


async def test_get_schema_hides_restricted_and_masks_pii(state: AppState) -> None:
    async with mem_client(state) as mem:
        out = await mem.call("get_schema")
        assert "payment_cards" not in out["tables"]
        assert out["hidden_restricted_tables"] == 1
        assert "payment_cards" not in out["ddl"]
        assert "<redacted>" in out["ddl"] and "@example.com" not in out["ddl"]
        assert "customers.email" in out["pii_columns"]
        assert out["dialect"] == "duckdb"


async def test_search_governance_cites(state: AppState) -> None:
    async with mem_client(state) as mem:
        out = await mem.call(
            "search_governance", {"query": "PII columns requiring approval", "k": 2}
        )
        assert len(out["passages"]) == 2
        top = out["passages"][0]
        assert top["citation"] == "policies.md#pii-columns"
        assert top["section"] == "Classification of data > PII columns"
        out = await mem.call("search_governance", {"query": "x", "k": 99})
        assert len(out["passages"]) == 10  # k is clamped


async def test_validate_sql_valid(state: AppState) -> None:
    async with mem_client(state) as mem:
        out = await mem.call("validate_sql", {"sql": "SELECT count(*) FROM orders"})
        assert out["valid"] is True and out["error"] is None
        assert "AGGREGATE" in out["plan"].upper()
        assert out["risk"]["level"] == "safe" and out["risk"]["tables"] == ["orders"]


async def test_validate_sql_invalid_sql_reports_duckdb_error(state: AppState) -> None:
    async with mem_client(state) as mem:
        out = await mem.call("validate_sql", {"sql": "SELECT nope FROM orders LIMIT 5"})
        assert out["valid"] is False and "nope" in out["error"]
        assert out["risk"]["level"] == "safe"  # risk is still computed


async def test_validate_sql_needs_approval(state: AppState) -> None:
    async with mem_client(state) as mem:
        out = await mem.call("validate_sql", {"sql": "SELECT email FROM customers LIMIT 5"})
        assert out["valid"] is True
        assert out["risk"]["level"] == "needs_approval"
        assert out["risk"]["sections"] == ["Classification of data > PII columns"]


async def test_validate_sql_blocked_is_not_planned(state: AppState) -> None:
    async with mem_client(state) as mem:
        out = await mem.call("validate_sql", {"sql": "SELECT * FROM payment_cards"})
        assert out["valid"] is False and out["plan"] is None
        assert out["risk"]["level"] == "blocked"
        assert "restricted" in out["error"]


async def test_run_sql_refuses_without_capability(state: AppState) -> None:
    async with mem_client(state) as mem:
        with pytest.raises(ToolCallError, match="capability"):
            await mem.call("run_sql", {"sql": "SELECT 1"})


# ---------------------------------------------------------------------------------------------
# Capability header over real Streamable HTTP
# ---------------------------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def http_url(state: AppState) -> Iterator[str]:
    port = _free_port()
    app = create_server(state).streamable_http_app(streamable_http_path="/mcp")
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    import time

    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    thread.join(timeout=5)


async def test_run_sql_with_capability_executes(http_url: str) -> None:
    async with tool_client(http_url, token=TOKEN) as c:
        out = await c.call(
            "run_sql",
            {"sql": "SELECT status, count(*) AS n FROM orders GROUP BY 1 ORDER BY 2 DESC"},
        )
    assert out["columns"] == ["status", "n"]
    assert out["row_count"] >= 3 and out["truncated"] is False
    assert out["risk_level"] == "needs_approval"  # no LIMIT: executing is the orchestrator's call


async def test_run_sql_serializes_dates_and_truncates(http_url: str) -> None:
    async with tool_client(http_url, token=TOKEN) as c:
        out = await c.call(
            "run_sql", {"sql": "SELECT order_date, order_ts FROM orders ORDER BY order_id"}
        )
    assert out["row_count"] == 50 and out["truncated"] is True
    assert out["rows"][0][0].startswith("202") and "T" in out["rows"][0][1]


async def test_run_sql_wrong_token_refused(http_url: str) -> None:
    async with tool_client(http_url, token="nope") as c:
        with pytest.raises(ToolCallError, match="capability"):
            await c.call("run_sql", {"sql": "SELECT 1"})


async def test_run_sql_no_token_over_http_refused(http_url: str) -> None:
    async with tool_client(http_url) as c:
        assert set(await c.tool_names()) == {*MODEL_TOOLS, "run_sql"}
        with pytest.raises(ToolCallError, match="capability"):
            await c.call("run_sql", {"sql": "SELECT 1"})


async def test_run_sql_blocked_even_with_token(http_url: str) -> None:
    async with tool_client(http_url, token=TOKEN) as c:
        with pytest.raises(ToolCallError, match="blocked by policy"):
            await c.call("run_sql", {"sql": "SELECT * FROM payment_cards"})
        with pytest.raises(ToolCallError, match="blocked by policy"):
            await c.call("run_sql", {"sql": "SELECT * FROM read_csv('/etc/passwd')"})


async def test_run_sql_execution_error_is_tool_error(http_url: str) -> None:
    async with tool_client(http_url, token=TOKEN) as c:
        with pytest.raises(ToolCallError, match="execution failed"):
            await c.call("run_sql", {"sql": "SELECT nope FROM orders LIMIT 1"})
