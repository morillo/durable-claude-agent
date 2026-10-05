"""AnalystWorkflow against a real local Temporal dev server with mocked activities.

These tests cover the orchestration itself: stage transitions, the deterministic risk gate, the
approval signal (approve / reject / timeout), blocked runs, search attributes, and that
``execute_sql`` is never called unless the gate opened. No LLM, no MCP server.
"""

from __future__ import annotations

import asyncio
import shutil
import uuid
from typing import Any

import pytest
from temporalio import activity
from temporalio.client import Client, WorkflowFailureError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from durable_agent.models import (
    AnalystRequest,
    AnalystResult,
    ApprovalDecision,
    ExecutionResult,
    GeneratedSQL,
    LLMUsage,
    Passage,
    QueryPlan,
    RetrievedContext,
    RiskResult,
    Stage,
    Summary,
)
from durable_agent.workflows import SEARCH_ATTRIBUTES, STAGE_KEY, AnalystWorkflow

pytestmark = pytest.mark.temporal

TQ = "test-analyst"


class FakeActivities:
    """Scripted activity results. ``risk_level`` steers the gate; ``calls`` records what ran."""

    def __init__(self, risk_level: str = "safe", sql: str = "SELECT count(*) FROM orders") -> None:
        self.risk_level = risk_level
        self.sql = sql
        self.calls: list[str] = []
        self.fail_generate_times = 0

    @activity.defn(name="retrieve_context")
    async def retrieve_context(self, request: AnalystRequest) -> RetrievedContext:
        self.calls.append("retrieve_context")
        p = Passage(
            source="policies.md",
            section="Query shape rules > Row limits",
            citation="policies.md#row-limits",
            score=0.9,
            text="Result sets are capped at 1000 rows.",
        )
        return RetrievedContext(
            passages=[p],
            schema_ddl="CREATE TABLE orders (order_id BIGINT);",
            pii_columns=["customers.email"],
        )

    @activity.defn(name="plan_query")
    async def plan_query(
        self, request: AnalystRequest, context: RetrievedContext
    ) -> tuple[QueryPlan, LLMUsage]:
        self.calls.append("plan_query")
        plan = QueryPlan(tables=["orders"], expected_output_shape="scalar", risk_assessment="safe")
        return plan, LLMUsage(
            model="claude-haiku-4-5", calls=1, input_tokens=100, output_tokens=20, cost_usd=0.0002
        )

    @activity.defn(name="generate_sql")
    async def generate_sql(
        self, request: AnalystRequest, context: RetrievedContext, plan: QueryPlan
    ) -> GeneratedSQL:
        self.calls.append("generate_sql")
        if self.fail_generate_times > 0:
            self.fail_generate_times -= 1
            raise RuntimeError("transient LLM error")
        return GeneratedSQL(
            sql=self.sql,
            explanation="counts orders",
            self_reported_risk="safe",
            tool_calls=["get_schema", "validate_sql"],
            validated=True,
            usage=LLMUsage(
                model="claude-sonnet-5-5",
                calls=2,
                input_tokens=2000,
                output_tokens=300,
                cost_usd=0.007,
            ),
        )

    @activity.defn(name="classify_risk")
    async def classify_risk(self, sql: str) -> RiskResult:
        self.calls.append("classify_risk")
        reasons = {
            "safe": [],
            "needs_approval": ["R4: touches PII column(s): customers.email"],
            "blocked": ["R3: table payment_cards is restricted"],
        }
        sections = {
            "safe": [],
            "needs_approval": ["Classification of data > PII columns"],
            "blocked": ["Classification of data > Restricted tables"],
        }
        return RiskResult(
            level=self.risk_level,
            reasons=reasons[self.risk_level],
            sections=sections[self.risk_level],
            tables=["orders"],
            limit=None,
            scalar_aggregate=True,
        )

    @activity.defn(name="execute_sql")
    async def execute_sql(self, sql: str) -> ExecutionResult:
        self.calls.append("execute_sql")
        return ExecutionResult(
            columns=["n"], rows=[[3000]], row_count=1, truncated=False, elapsed_ms=3
        )

    @activity.defn(name="summarize")
    async def summarize(
        self,
        request: AnalystRequest,
        sql: str,
        result: ExecutionResult,
        citations: list[str],
        risk_level: str,
    ) -> tuple[Summary, LLMUsage]:
        self.calls.append("summarize")
        return Summary(
            answer="There are 3000 orders.", caveats=["all statuses"], citations=citations
        ), LLMUsage(
            model="claude-haiku-4-5", calls=1, input_tokens=300, output_tokens=40, cost_usd=0.0005
        )

    @property
    def all(self) -> list[Any]:
        return [
            self.retrieve_context,
            self.plan_query,
            self.generate_sql,
            self.classify_risk,
            self.execute_sql,
            self.summarize,
        ]


@pytest.fixture(scope="module")
def env_and_client() -> Any:
    """One dev server per module (startup is ~1s). Uses the brew-installed CLI when present."""

    async def start() -> WorkflowEnvironment:
        return await WorkflowEnvironment.start_local(
            data_converter=pydantic_data_converter,
            search_attributes=SEARCH_ATTRIBUTES,
            dev_server_existing_path=shutil.which("temporal"),
            dev_server_log_level="error",
        )

    loop = asyncio.new_event_loop()
    env = loop.run_until_complete(start())
    yield loop, env
    loop.run_until_complete(env.shutdown())
    loop.close()


def run(loop: asyncio.AbstractEventLoop, coro: Any) -> Any:
    return loop.run_until_complete(coro)


async def _run_workflow(
    client: Client,
    acts: FakeActivities,
    request: AnalystRequest,
    *,
    signal: ApprovalDecision | None = None,
) -> tuple[AnalystResult, list[str]]:
    async with Worker(client, task_queue=TQ, workflows=[AnalystWorkflow], activities=acts.all):
        handle = await client.start_workflow(
            AnalystWorkflow.run, request, id=f"t-{uuid.uuid4().hex[:8]}", task_queue=TQ
        )
        if signal is not None:
            # wait until the gate is reached, then decide
            for _ in range(200):
                st = await handle.query(AnalystWorkflow.state)
                if st.stage == Stage.AWAITING_APPROVAL:
                    break
                await asyncio.sleep(0.05)
            else:
                raise AssertionError("workflow never reached awaiting_approval")
            assert st.sql and st.risk and st.risk.level == "needs_approval"
            await handle.signal(AnalystWorkflow.approve, signal)
        result = await handle.result()
        desc = await handle.describe()
        stage_attr = desc.typed_search_attributes.get(STAGE_KEY)
        return result, [stage_attr or ""]


def test_safe_path_executes_and_summarizes(env_and_client: Any) -> None:
    loop, env = env_and_client
    acts = FakeActivities("safe")
    result, sa = run(
        loop,
        _run_workflow(
            env.client, acts, AnalystRequest(question="How many orders?", requester="ana")
        ),
    )
    assert result.stage == Stage.COMPLETED
    assert acts.calls == [
        "retrieve_context",
        "plan_query",
        "generate_sql",
        "classify_risk",
        "execute_sql",
        "summarize",
    ]
    assert result.summary and "3000" in result.summary.answer
    assert result.approval is None
    assert result.usage.calls == 4 and abs(result.usage.cost_usd - 0.0077) < 1e-9
    assert result.citations == ["policies.md#row-limits"]
    assert sa == ["completed"]


def test_needs_approval_then_approved(env_and_client: Any) -> None:
    loop, env = env_and_client
    acts = FakeActivities("needs_approval", sql="SELECT email FROM customers LIMIT 5")
    decision = ApprovalDecision(approved=True, note="marketing audit, ticket 123", decided_by="dpo")
    result, sa = run(
        loop,
        _run_workflow(
            env.client, acts, AnalystRequest(question="emails?", requester="ana"), signal=decision
        ),
    )
    assert result.stage == Stage.COMPLETED
    assert result.approval == decision
    assert "execute_sql" in acts.calls
    assert "policies.md#pii-columns" in result.citations
    assert sa == ["completed"]


def test_needs_approval_then_rejected_never_executes(env_and_client: Any) -> None:
    loop, env = env_and_client
    acts = FakeActivities("needs_approval", sql="SELECT email FROM customers LIMIT 5")
    decision = ApprovalDecision(approved=False, note="no business need", decided_by="dpo")
    result, sa = run(
        loop,
        _run_workflow(
            env.client, acts, AnalystRequest(question="emails?", requester="ana"), signal=decision
        ),
    )
    assert result.stage == Stage.REJECTED
    assert "execute_sql" not in acts.calls and "summarize" not in acts.calls
    assert result.error and "no business need" in result.error
    assert sa == ["rejected"]


def test_approval_timeout_never_executes(env_and_client: Any) -> None:
    loop, env = env_and_client
    acts = FakeActivities("needs_approval", sql="SELECT email FROM customers LIMIT 5")
    req = AnalystRequest(question="emails?", requester="ana", approval_timeout_seconds=1)
    result, sa = run(loop, _run_workflow(env.client, acts, req))
    assert result.stage == Stage.APPROVAL_TIMEOUT
    assert "execute_sql" not in acts.calls
    assert sa == ["approval_timeout"]


def test_blocked_ends_without_approval_or_execution(env_and_client: Any) -> None:
    loop, env = env_and_client
    acts = FakeActivities("blocked", sql="SELECT * FROM payment_cards")
    result, sa = run(
        loop, _run_workflow(env.client, acts, AnalystRequest(question="cards?", requester="ana"))
    )
    assert result.stage == Stage.BLOCKED
    assert acts.calls[-1] == "classify_risk"
    assert result.error and "restricted" in result.error
    assert "policies.md#restricted-tables" in result.citations
    assert sa == ["blocked"]


def test_transient_activity_failure_is_retried(env_and_client: Any) -> None:
    loop, env = env_and_client
    acts = FakeActivities("safe")
    acts.fail_generate_times = 2  # first two attempts raise; retry policy allows 4
    result, _ = run(
        loop,
        _run_workflow(
            env.client, acts, AnalystRequest(question="How many orders?", requester="ana")
        ),
    )
    assert result.stage == Stage.COMPLETED
    assert acts.calls.count("generate_sql") == 3
    assert acts.calls.count("plan_query") == 1  # completed activities are not re-run


def test_signal_is_idempotent_first_decision_wins(env_and_client: Any) -> None:
    loop, env = env_and_client
    acts = FakeActivities("needs_approval", sql="SELECT email FROM customers LIMIT 5")

    async def go() -> AnalystResult:
        async with Worker(
            env.client, task_queue=TQ, workflows=[AnalystWorkflow], activities=acts.all
        ):
            handle = await env.client.start_workflow(
                AnalystWorkflow.run,
                AnalystRequest(question="emails?", requester="ana"),
                id=f"t-{uuid.uuid4().hex[:8]}",
                task_queue=TQ,
            )
            for _ in range(200):
                if (await handle.query(AnalystWorkflow.state)).stage == Stage.AWAITING_APPROVAL:
                    break
                await asyncio.sleep(0.05)
            await handle.signal(
                AnalystWorkflow.approve,
                ApprovalDecision(approved=False, note="first", decided_by="a"),
            )
            await handle.signal(
                AnalystWorkflow.approve,
                ApprovalDecision(approved=True, note="second", decided_by="b"),
            )
            return await handle.result()

    result = run(loop, go())
    assert result.stage == Stage.REJECTED and result.approval and result.approval.note == "first"


def test_unrecoverable_activity_error_fails_workflow_with_reason(env_and_client: Any) -> None:
    loop, env = env_and_client
    acts = FakeActivities("safe")
    acts.fail_generate_times = 10  # exceeds maximum_attempts=4

    async def go() -> None:
        async with Worker(
            env.client, task_queue=TQ, workflows=[AnalystWorkflow], activities=acts.all
        ):
            handle = await env.client.start_workflow(
                AnalystWorkflow.run,
                AnalystRequest(question="how many?", requester="ana"),
                id=f"t-{uuid.uuid4().hex[:8]}",
                task_queue=TQ,
            )
            with pytest.raises(WorkflowFailureError) as ei:
                await handle.result()
            assert isinstance(ei.value.cause, ApplicationError)
            assert "transient LLM error" in str(ei.value.cause)

    run(loop, go())
    assert acts.calls.count("generate_sql") == 4
