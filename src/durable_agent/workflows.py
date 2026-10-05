"""``AnalystWorkflow``: the durable orchestration of one analyst question.

What Temporal gives this code
-----------------------------
* Every ``execute_activity`` call has a timeout and a retry policy. An LLM timeout, a tool
  error, or a worker crash becomes a retry, not a lost run.
* The workflow's own state (plan, SQL, risk, approval) lives in event history. If the worker
  dies, a new worker replays history and continues from the last completed step. Completed
  activities are never re-executed.
* The approval gate is ``wait_condition`` on a signal with a timeout. The run can sit for hours
  with no process holding memory for it.
* ``@workflow.query`` exposes live state to the CLI and the Web UI; search attributes make runs
  filterable ("show me everything awaiting approval").

What Temporal does not give for free: an interrupted *activity* may run again from the start.
``generate_sql`` handles that with heartbeat checkpoints, and the LLM response cache makes any
repeated API call free. See docs/ARCHITECTURE.md -> Failure modes.

Sandbox note: workflow code must be deterministic, so this module imports only the models and
Temporal; the heavy libraries live in activities.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy, SearchAttributeKey
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from durable_agent.models import (
        AnalystRequest,
        AnalystResult,
        ApprovalDecision,
        ExecutionResult,
        GeneratedSQL,
        LLMUsage,
        QueryPlan,
        RetrievedContext,
        RiskResult,
        Stage,
        Summary,
    )

# Registered on the dev server by `make temporal`; tests pass them to start_local().
STAGE_KEY = SearchAttributeKey.for_keyword("AnalystStage")
RISK_KEY = SearchAttributeKey.for_keyword("AnalystRisk")
REQUESTER_KEY = SearchAttributeKey.for_keyword("AnalystRequester")
SEARCH_ATTRIBUTES = (STAGE_KEY, RISK_KEY, REQUESTER_KEY)

TASK_QUEUE_DEFAULT = "analyst"

# Retry policies: LLM/tool calls get a few attempts with backoff; deterministic steps need one.
LLM_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=4,
    # Misconfiguration and policy refusals will not get better on retry.
    non_retryable_error_types=[
        "LLMRefusalError",
        "ValueError",
        "TypeError",
        "AuthenticationError",
        "PermissionDeniedError",
        "BadRequestError",
        "NotFoundError",
    ],
)
TOOL_RETRY = RetryPolicy(initial_interval=timedelta(seconds=1), maximum_attempts=3)
NO_RETRY = RetryPolicy(maximum_attempts=1)


@workflow.defn
class AnalystWorkflow:
    def __init__(self) -> None:
        self._state: AnalystResult | None = None
        self._approval: ApprovalDecision | None = None

    # -- external surface ---------------------------------------------------------------------

    @workflow.signal
    async def approve(self, decision: ApprovalDecision) -> None:
        """Human decision for a ``needs_approval`` run. Idempotent: first decision wins."""
        if self._approval is None:
            self._approval = decision

    @workflow.query
    def state(self) -> AnalystResult:
        assert self._state is not None
        return self._state

    # -- main ---------------------------------------------------------------------------------

    @workflow.run
    async def run(self, request: AnalystRequest) -> AnalystResult:
        self._state = AnalystResult(
            stage=Stage.STARTED, question=request.question, requester=request.requester
        )
        workflow.upsert_search_attributes([REQUESTER_KEY.value_set(request.requester)])
        try:
            return await self._run(request)
        except ActivityError as e:
            cause = e.cause or e
            self._set_stage(Stage.FAILED)
            self._state.error = f"{type(cause).__name__}: {cause}"
            raise ApplicationError(self._state.error, non_retryable=True) from e

    async def _run(self, request: AnalystRequest) -> AnalystResult:
        assert self._state is not None
        state = self._state

        # 1. retrieve (zero tokens)
        self._set_stage(Stage.RETRIEVING)
        context: RetrievedContext = await workflow.execute_activity(
            "retrieve_context",
            request,
            result_type=RetrievedContext,
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=TOOL_RETRY,
        )
        state.retrieved_citations = [p.citation for p in context.passages]
        state.citations = list(state.retrieved_citations)

        # 2. plan (fast model)
        self._set_stage(Stage.PLANNING)
        plan, plan_usage = await workflow.execute_activity(
            "plan_query",
            args=[request, context],
            result_type=tuple[QueryPlan, LLMUsage],
            start_to_close_timeout=timedelta(seconds=90),
            retry_policy=LLM_RETRY,
        )
        state.plan, state.usage = plan, state.usage.add(plan_usage)

        # 3. generate SQL with tools (strong model). Heartbeats make the crash demo recoverable:
        #    if no heartbeat arrives for heartbeat_timeout, the server retries on a live worker.
        self._set_stage(Stage.GENERATING)
        gen: GeneratedSQL = await workflow.execute_activity(
            "generate_sql",
            args=[request, context, plan],
            result_type=GeneratedSQL,
            start_to_close_timeout=timedelta(minutes=5),
            heartbeat_timeout=timedelta(seconds=10),
            retry_policy=LLM_RETRY,
        )
        state.sql, state.explanation = gen.sql, gen.explanation
        state.tool_calls, state.resumed_from_checkpoint = (
            gen.tool_calls,
            gen.resumed_from_checkpoint,
        )
        state.sql_validated = gen.validated
        state.usage = state.usage.add(gen.usage)

        # 4. deterministic risk classification: the only signal that gates execution
        self._set_stage(Stage.CLASSIFYING)
        risk: RiskResult = await workflow.execute_activity(
            "classify_risk",
            gen.sql,
            result_type=RiskResult,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=NO_RETRY,
        )
        state.risk = risk
        workflow.upsert_search_attributes([RISK_KEY.value_set(risk.level)])
        state.citations = sorted({*state.citations, *_sections_to_citations(risk.sections)})

        if risk.level == "blocked":
            self._set_stage(Stage.BLOCKED)
            state.error = "blocked by policy: " + "; ".join(risk.reasons)
            return state

        # 5. human approval gate
        if risk.level == "needs_approval":
            self._set_stage(Stage.AWAITING_APPROVAL)
            try:
                await workflow.wait_condition(
                    lambda: self._approval is not None,
                    timeout=timedelta(seconds=request.approval_timeout_seconds),
                )
            except TimeoutError:
                self._set_stage(Stage.APPROVAL_TIMEOUT)
                state.error = f"no approval decision within {request.approval_timeout_seconds}s"
                return state
            assert self._approval is not None
            state.approval = self._approval
            if not self._approval.approved:
                self._set_stage(Stage.REJECTED)
                state.error = (
                    f"rejected by {self._approval.decided_by or 'approver'}: {self._approval.note}"
                )
                return state

        # 6. execute (capability-token path)
        self._set_stage(Stage.EXECUTING)
        result: ExecutionResult = await workflow.execute_activity(
            "execute_sql",
            gen.sql,
            result_type=ExecutionResult,
            start_to_close_timeout=timedelta(seconds=120),
            retry_policy=TOOL_RETRY,
        )
        state.result = result

        # 7. summarize (fast model)
        self._set_stage(Stage.SUMMARIZING)
        summary, sum_usage = await workflow.execute_activity(
            "summarize",
            args=[request, gen.sql, result, state.citations, risk.level],
            result_type=tuple[Summary, LLMUsage],
            start_to_close_timeout=timedelta(seconds=90),
            retry_policy=LLM_RETRY,
        )
        state.summary, state.usage = summary, state.usage.add(sum_usage)
        self._set_stage(Stage.COMPLETED)
        workflow.logger.info(
            "run complete: %d Claude calls, %d in / %d out tokens, est. cost $%.4f",
            state.usage.calls,
            state.usage.input_tokens,
            state.usage.output_tokens,
            state.usage.cost_usd,
        )
        return state

    # -- helpers ------------------------------------------------------------------------------

    def _set_stage(self, stage: Stage) -> None:
        assert self._state is not None
        self._state.stage = stage
        workflow.upsert_search_attributes([STAGE_KEY.value_set(stage.value)])
        workflow.logger.info("stage=%s", stage.value)


def _sections_to_citations(sections: list[str]) -> list[str]:
    """Map 'Query shape rules > Row limits' to 'policies.md#row-limits' (chunking's slug rule)."""
    out = []
    for s in sections:
        leaf = s.split(" > ")[-1].lower()
        slug = (
            "".join(ch if ch.isalnum() or ch in " -_" else "" for ch in leaf)
            .strip()
            .replace(" ", "-")
        )
        out.append(f"policies.md#{slug}")
    return out
