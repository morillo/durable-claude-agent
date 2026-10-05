"""The MCP server: tool definitions and the capability check for ``run_sql``.

Tool-side enforcement versus trusting the model
------------------------------------------------
A system prompt that says "never call run_sql" is a request, not a control. Models follow
instructions most of the time; security needs "always". So authorization lives where the
action happens:

1. ``run_sql`` reads the ``Authorization`` header from the transport and compares it in
   constant time to a secret only the Temporal worker's ``execute_sql`` activity holds. The
   model-facing tool session has no such header, so the call fails even if the model asks.
2. ``run_sql`` re-runs the deterministic classifier and refuses ``blocked`` SQL regardless of
   the token. A compromised or buggy caller still cannot read ``payment_cards``.
3. The schema the model sees already hides restricted tables and redacts PII samples, so the
   least-privilege view is the default, not an afterthought.

Headers exist only over HTTP transports (``ctx.headers`` is ``None`` on stdio), which is why
this server runs over Streamable HTTP rather than stdio.
"""

from __future__ import annotations

import datetime as dt
import decimal
import hmac
import threading
from dataclasses import dataclass, field
from typing import Any

import anyio
import duckdb
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from durable_agent.config import Settings
from durable_agent.lakehouse import Lakehouse, QueryTimeoutError
from durable_agent.retrieval import Embedder, GovernanceIndex, SentenceTransformerEmbedder
from durable_agent.risk import Policy, RiskLevel, classify, load_policy

MODEL_TOOLS: tuple[str, ...] = ("get_schema", "search_governance", "validate_sql")
PRIVILEGED_TOOLS: tuple[str, ...] = ("run_sql",)
READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)


# -- structured outputs (these become the tools' output schemas) -------------------------------


class SchemaResult(BaseModel):
    ddl: str = Field(
        description="CREATE TABLE text for every queryable table, with redacted sample rows."
    )
    tables: list[str]
    pii_columns: list[str] = Field(description="table.column names that require approval to touch.")
    hidden_restricted_tables: int = Field(
        description="Count of restricted tables omitted from this view."
    )
    dialect: str = "duckdb"


class PassageOut(BaseModel):
    source: str
    section: str
    anchor: str
    citation: str
    score: float
    text: str


class SearchResult(BaseModel):
    query: str
    passages: list[PassageOut]


class RiskOut(BaseModel):
    level: str
    reasons: list[str]
    sections: list[str]
    tables: list[str]
    limit: int | None
    scalar_aggregate: bool


class ValidationResult(BaseModel):
    valid: bool
    error: str | None = None
    plan: str | None = Field(
        default=None, description="DuckDB EXPLAIN output when the statement planned."
    )
    risk: RiskOut


class RunResult(BaseModel):
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    elapsed_ms: int
    risk_level: str


# -- server --------------------------------------------------------------------------------------


@dataclass
class AppState:
    """Everything the tools need, built once per process."""

    settings: Settings
    lakehouse: Lakehouse
    policy: Policy
    index: GovernanceIndex
    lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def from_settings(cls, settings: Settings, embedder: Embedder | None = None) -> AppState:
        embedder = embedder or SentenceTransformerEmbedder(settings.embedding_model)
        return cls(
            settings=settings,
            lakehouse=Lakehouse(settings.lakehouse_path),
            policy=load_policy(settings.governance_path / "policies.md"),
            index=GovernanceIndex(settings.index_path, embedder),
        )


def _json_safe(value: Any) -> Any:
    if isinstance(value, dt.datetime | dt.date | dt.time):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.hex()
    return value


def _risk_out(sql: str, state: AppState) -> RiskOut:
    a = classify(sql, state.lakehouse.schema(), state.policy)
    return RiskOut(
        level=a.level.value,
        reasons=[f"{r.rule}: {r.message}" for r in a.reasons],
        sections=list(a.sections),
        tables=list(a.tables),
        limit=a.limit,
        scalar_aggregate=a.scalar_aggregate,
    )


def _bearer(ctx: Context) -> str | None:
    headers = ctx.headers or {}
    value = headers.get("authorization") or headers.get("Authorization")
    if value and value.lower().startswith("bearer "):
        return value[7:].strip()
    return None


def create_server(state: AppState) -> MCPServer:
    """Build the MCPServer with the four tools closed over ``state``."""
    mcp = MCPServer(
        name="governed-lakehouse",
        version="0.1.0",
        instructions=(
            "Tools over a governed retail lakehouse (DuckDB dialect). Call get_schema before "
            "writing SQL, search_governance for business definitions and policy rules, and "
            "validate_sql to dry-run a statement and see its risk class. run_sql is reserved for "
            "the orchestrator and will refuse unauthorized callers."
        ),
    )
    settings = state.settings

    async def _run_blocking(fn: Any, *args: Any) -> Any:
        # DuckDB connections are not safe for concurrent queries; serialize access.
        def locked() -> Any:
            with state.lock:
                return fn(*args)

        return await anyio.to_thread.run_sync(locked)

    @mcp.tool(annotations=READ_ONLY)
    async def get_schema() -> SchemaResult:
        """Return the queryable schema: CREATE TABLE text plus redacted sample rows.

        Restricted tables are omitted and PII sample values are redacted. Use exactly these
        table and column names; the dialect is DuckDB.
        """
        ddl = await _run_blocking(
            lambda: state.lakehouse.describe_schema(
                exclude=state.policy.restricted_tables, mask_columns=state.policy.pii_columns
            )
        )
        tables = [t for t in state.lakehouse.tables if t not in state.policy.restricted_tables]
        return SchemaResult(
            ddl=ddl,
            tables=tables,
            pii_columns=sorted(state.policy.pii_columns),
            hidden_restricted_tables=len(state.lakehouse.tables) - len(tables),
        )

    @mcp.tool(annotations=READ_ONLY)
    async def search_governance(query: str, k: int = 4) -> SearchResult:
        """Search the data dictionary and governance policies.

        Returns the top-k passages with a citation (file#anchor) for each. Use it to find
        metric definitions (e.g. how net revenue is computed) and rules about PII, restricted
        tables, and row limits before writing SQL.
        """
        k = max(1, min(int(k), 10))
        passages = await anyio.to_thread.run_sync(state.index.search, query, k)
        return SearchResult(
            query=query,
            passages=[
                PassageOut(
                    source=p.source,
                    section=p.section,
                    anchor=p.anchor,
                    citation=p.citation,
                    score=p.score,
                    text=p.text,
                )
                for p in passages
            ],
        )

    @mcp.tool(annotations=READ_ONLY)
    async def validate_sql(sql: str) -> ValidationResult:
        """Dry-run a SQL statement: plan it with EXPLAIN and classify its governance risk.

        Nothing is executed. ``risk.level`` is one of safe, needs_approval, blocked; fix any
        reasons you can (add a LIMIT, drop PII columns) and validate again. Blocked statements
        are not planned.
        """
        risk = await _run_blocking(_risk_out, sql, state)
        if risk.level == RiskLevel.BLOCKED.value:
            return ValidationResult(
                valid=False, error="blocked by policy: " + "; ".join(risk.reasons), risk=risk
            )
        try:
            plan = await _run_blocking(state.lakehouse.explain, sql)
        except duckdb.Error as e:
            return ValidationResult(valid=False, error=str(e).strip(), risk=risk)
        return ValidationResult(valid=True, plan=plan, risk=risk)

    @mcp.tool(
        annotations=ToolAnnotations(
            read_only_hint=True, destructive_hint=False, idempotent_hint=True
        ),
        meta={"requires_capability": "run_sql"},
    )
    async def run_sql(sql: str, ctx: Context) -> RunResult:
        """Execute a read-only SQL statement. Requires the run_sql capability (bearer token).

        Orchestrator-only. Calls without a valid Authorization header are refused; blocked
        statements are refused regardless of credentials. Results are capped at the configured
        row limit and statement timeout.
        """
        expected = settings.run_sql_capability_token
        presented = _bearer(ctx)
        if (
            expected is None
            or presented is None
            or not hmac.compare_digest(presented.encode(), expected.get_secret_value().encode())
        ):
            raise ToolError("run_sql requires the run_sql capability; this caller does not hold it")
        risk = await _run_blocking(_risk_out, sql, state)
        if risk.level == RiskLevel.BLOCKED.value:
            raise ToolError("refused: statement is blocked by policy: " + "; ".join(risk.reasons))
        try:
            result = await _run_blocking(
                lambda: state.lakehouse.execute(
                    sql, max_rows=settings.max_rows, timeout_s=settings.query_timeout_seconds
                )
            )
        except QueryTimeoutError as e:
            raise ToolError(str(e)) from e
        except duckdb.Error as e:
            raise ToolError(f"execution failed: {str(e).strip()}") from e
        return RunResult(
            columns=result.columns,
            rows=[[_json_safe(v) for v in row] for row in result.rows],
            row_count=result.row_count,
            truncated=result.truncated,
            elapsed_ms=result.elapsed_ms,
            risk_level=risk.level,
        )

    return mcp
