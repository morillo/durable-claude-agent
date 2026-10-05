"""Types that cross the workflow/activity boundary.

Temporal serializes every activity input and output. Using pydantic models (with the SDK's
``pydantic_data_converter``) gives schema-checked payloads that show up as readable JSON in the
Temporal Web UI, which is half the point of the demo: anyone can open a run and read the plan,
the SQL, the risk reasons, and the approval note without a debugger.

Nothing in here imports DuckDB, Anthropic, or MCP, so the workflow sandbox can import it.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class Stage(StrEnum):
    """Workflow stages; mirrored into the ``AnalystStage`` search attribute."""

    STARTED = "started"
    RETRIEVING = "retrieving"
    PLANNING = "planning"
    GENERATING = "generating"
    CLASSIFYING = "classifying"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    SUMMARIZING = "summarizing"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    REJECTED = "rejected"
    APPROVAL_TIMEOUT = "approval_timeout"
    FAILED = "failed"


class AnalystRequest(BaseModel):
    question: str = Field(min_length=3)
    requester: str = Field(
        min_length=1, description="Who is asking; recorded on the run and in the approval note."
    )
    approval_timeout_seconds: int = Field(
        default=24 * 3600, ge=1, description="How long to wait for a human decision on risky SQL."
    )


class LLMUsage(BaseModel):
    """Token accounting for one or more calls, with an estimated cost."""

    model: str
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    cached_responses: int = Field(
        default=0, description="Calls served from the local response cache."
    )

    def add(self, other: LLMUsage) -> LLMUsage:
        return LLMUsage(
            model=self.model if self.model == other.model else "mixed",
            calls=self.calls + other.calls,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            cost_usd=round(self.cost_usd + other.cost_usd, 6),
            cached_responses=self.cached_responses + other.cached_responses,
        )


class Passage(BaseModel):
    source: str
    section: str
    citation: str
    score: float
    text: str


class RetrievedContext(BaseModel):
    passages: list[Passage]
    schema_ddl: str = Field(
        description="Redacted DDL for queryable tables (restricted tables omitted)."
    )
    pii_columns: list[str]


class QueryPlan(BaseModel):
    """Structured output of ``plan_query`` (Claude, JSON schema enforced)."""

    tables: list[str] = Field(description="Tables needed, by exact name.")
    joins: list[str] = Field(default_factory=list, description="Join conditions in SQL form.")
    filters: list[str] = Field(default_factory=list, description="WHERE predicates in SQL form.")
    metrics: list[str] = Field(
        default_factory=list, description="Aggregations or columns to return."
    )
    expected_output_shape: str = Field(
        description="e.g. 'one row per country, 8 rows' or 'single scalar'."
    )
    assumptions: list[str] = Field(
        default_factory=list, description="Ambiguities and how they were resolved."
    )
    governance_sections_used: list[str] = Field(
        default_factory=list, description="Citations relied on."
    )
    risk_assessment: str = Field(
        description="One of: safe, needs_approval, blocked, with a short reason."
    )


class GeneratedSQL(BaseModel):
    sql: str
    explanation: str
    self_reported_risk: str = Field(
        description="Model's own view: safe | needs_approval | blocked. Recorded, not trusted."
    )
    tool_calls: list[str] = Field(
        default_factory=list, description="Tool names in call order (for the tool-use judge)."
    )
    validated: bool = Field(
        default=False, description="Whether validate_sql returned valid=True for the final SQL."
    )
    usage: LLMUsage
    resumed_from_checkpoint: bool = Field(
        default=False, description="True if this attempt resumed from heartbeat details."
    )


class RiskResult(BaseModel):
    level: str
    reasons: list[str]
    sections: list[str]
    tables: list[str]
    limit: int | None
    scalar_aggregate: bool


class ExecutionResult(BaseModel):
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    elapsed_ms: int


class Summary(BaseModel):
    answer: str = Field(description="Plain-English answer with the key numbers.")
    caveats: list[str] = Field(
        default_factory=list, description="Assumptions, date ranges, exclusions applied."
    )
    citations: list[str] = Field(
        default_factory=list, description="Governance citations used, as file#anchor."
    )


class ApprovalDecision(BaseModel):
    approved: bool
    note: str = ""
    decided_by: str = ""


class AnalystResult(BaseModel):
    """Final workflow result. Also what the ``state`` query returns while running."""

    stage: Stage
    question: str
    requester: str
    plan: QueryPlan | None = None
    sql: str | None = None
    explanation: str | None = None
    risk: RiskResult | None = None
    approval: ApprovalDecision | None = None
    result: ExecutionResult | None = None
    summary: Summary | None = None
    citations: list[str] = Field(
        default_factory=list, description="Merged: retrieval + policy sections."
    )
    retrieved_citations: list[str] = Field(
        default_factory=list, description="From retrieve_context only."
    )
    usage: LLMUsage = Field(default_factory=lambda: LLMUsage(model="none"))
    error: str | None = None
    tool_calls: list[str] = Field(default_factory=list)
    sql_validated: bool = False
    resumed_from_checkpoint: bool = False
