"""Temporal activities: one per agent step.

Each activity is a plain async function on ``AnalystActivities`` with its dependencies injected
once at worker start. Rules every activity follows:

* **Idempotent.** Temporal may run an activity more than once (worker crash, timeout). Reads
  are naturally idempotent; LLM calls are made idempotent by the response cache in ``llm.py``;
  ``execute_sql`` only ever runs read-only SQL.
* **Resumable where long.** ``generate_sql`` checkpoints its conversation into heartbeat
  details after every tool round. A retry resumes from the last checkpoint instead of
  re-running earlier Claude turns. This is the Temporal-native idiom for "don't redo work".
* **No orchestration decisions.** Activities return data; the workflow decides what happens
  next (approval, block, execute). That keeps control flow in one replayable place.

The crash hook (``DEMO_CRASH_AT=generate_sql``) kills the worker process right after the first
tool call on the first attempt, to demonstrate recovery. It is inert unless the env var is set.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from dataclasses import dataclass
from typing import Any

from temporalio import activity

from durable_agent.config import Settings
from durable_agent.lakehouse import Lakehouse
from durable_agent.llm import LLM, text_of
from durable_agent.mcp_server import MODEL_TOOLS, tool_client
from durable_agent.mcp_server.client import ToolCallError
from durable_agent.models import (
    AnalystRequest,
    ExecutionResult,
    GeneratedSQL,
    LLMUsage,
    Passage,
    QueryPlan,
    RetrievedContext,
    RiskResult,
    Summary,
)
from durable_agent.prompts import (
    GENERATE_SYSTEM,
    PLAN_SYSTEM,
    SUMMARIZE_SYSTEM,
    generate_user,
    plan_user,
    summarize_user,
)
from durable_agent.retrieval import Embedder, GovernanceIndex, SentenceTransformerEmbedder
from durable_agent.risk import Policy, classify, load_policy

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 8
HEARTBEAT_PULSE_SECONDS = 3
FINAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "sql": {"type": "string"},
        "explanation": {"type": "string"},
        "self_reported_risk": {"type": "string", "enum": ["safe", "needs_approval", "blocked"]},
    },
    "required": ["sql", "explanation", "self_reported_risk"],
    "additionalProperties": False,
}


@dataclass
class AnalystActivities:
    settings: Settings
    llm: LLM
    lakehouse: Lakehouse
    policy: Policy
    index: GovernanceIndex

    @classmethod
    def from_settings(
        cls, settings: Settings, embedder: Embedder | None = None
    ) -> AnalystActivities:
        embedder = embedder or SentenceTransformerEmbedder(settings.embedding_model)
        return cls(
            settings=settings,
            llm=LLM(settings),
            lakehouse=Lakehouse(settings.lakehouse_path),
            policy=load_policy(settings.governance_path / "policies.md"),
            index=GovernanceIndex(settings.index_path, embedder),
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

    # 1 ------------------------------------------------------------------------------------
    @activity.defn
    async def retrieve_context(self, request: AnalystRequest) -> RetrievedContext:
        """Top-k governance passages plus the redacted schema. Zero tokens."""
        hits = self.index.search(request.question, k=6)
        # Always include the sections the SQL step must respect even when the question does not
        # mention them: the two policy rules every query is checked against, and the one
        # business rule (cancelled orders) that silently changes almost every metric.
        must_have = {
            "policies.md#pii-columns",
            "policies.md#row-limits",
            "data_dictionary.md#cancelled-orders",
        }
        extra = [
            p
            for p in self.index.search(
                "PII columns row limits approval cancelled orders excluded", k=10
            )
            if p.citation in must_have
        ]
        seen: dict[str, Passage] = {}
        for p in [*hits, *extra]:
            seen.setdefault(
                p.citation,
                Passage(
                    source=p.source,
                    section=p.section,
                    citation=p.citation,
                    score=p.score,
                    text=p.text,
                ),
            )
        ddl = self.lakehouse.describe_schema(
            exclude=self.policy.restricted_tables, mask_columns=self.policy.pii_columns
        )
        return RetrievedContext(
            passages=list(seen.values()),
            schema_ddl=ddl,
            pii_columns=sorted(self.policy.pii_columns),
        )

    # 2 ------------------------------------------------------------------------------------
    @activity.defn
    async def plan_query(
        self, request: AnalystRequest, context: RetrievedContext
    ) -> tuple[QueryPlan, LLMUsage]:
        """Fast model, structured output: tables, joins, filters, output shape, risk view."""
        plan, usage = await self.llm.structured(
            model=self.settings.model_fast,
            system=PLAN_SYSTEM.format(schema=context.schema_ddl),
            user=plan_user(request.question, [(p.citation, p.text) for p in context.passages]),
            schema=QueryPlan,
        )
        return plan, usage

    # 3 ------------------------------------------------------------------------------------
    @activity.defn
    async def generate_sql(
        self, request: AnalystRequest, context: RetrievedContext, plan: QueryPlan
    ) -> GeneratedSQL:
        """Strong model with MCP tools; manual loop so every round can be checkpointed."""
        info = activity.info()
        checkpoint = info.heartbeat_details[0] if info.heartbeat_details else None
        resumed = checkpoint is not None

        # A single Claude turn can outlast the heartbeat timeout, so a background pulse re-sends
        # the latest checkpoint every few seconds while the activity is alive. The explicit
        # heartbeat after each tool round updates what the pulse sends.
        latest: dict[str, Any] = {"ckpt": checkpoint}

        async def pulse() -> None:
            while True:
                await asyncio.sleep(HEARTBEAT_PULSE_SECONDS)
                if latest["ckpt"] is not None:
                    activity.heartbeat(latest["ckpt"])
                else:
                    activity.heartbeat()

        pulse_task = asyncio.create_task(pulse())
        try:
            return await self._generate_sql(request, context, plan, checkpoint, resumed, latest)
        finally:
            pulse_task.cancel()

    async def _generate_sql(
        self,
        request: AnalystRequest,
        context: RetrievedContext,
        plan: QueryPlan,
        checkpoint: dict[str, Any] | None,
        resumed: bool,
        latest: dict[str, Any],
    ) -> GeneratedSQL:
        info = activity.info()

        async with tool_client(self.settings.mcp_server_url) as tools:
            mcp_tools = {
                t.name: t for t in (await tools.raw.list_tools()).tools if t.name in MODEL_TOOLS
            }
            tool_defs = [
                {"name": t.name, "description": t.description or "", "input_schema": t.input_schema}
                for t in sorted(
                    mcp_tools.values(), key=lambda t: t.name
                )  # stable order = cache hits
            ]

            if checkpoint:
                messages: list[dict[str, Any]] = checkpoint["messages"]
                tool_calls: list[str] = checkpoint["tool_calls"]
                usage = LLMUsage.model_validate(checkpoint["usage"])
                validated = bool(checkpoint["validated"])
                activity.logger.info(
                    "generate_sql resumed from checkpoint after %d tool call(s)", len(tool_calls)
                )
            else:
                messages = [
                    {
                        "role": "user",
                        "content": generate_user(
                            request.question,
                            plan.model_dump_json(indent=1),
                            [(p.citation, p.text) for p in context.passages],
                        ),
                    }
                ]
                tool_calls, usage, validated = [], LLMUsage(model=self.settings.model_strong), False

            final: dict[str, Any] | None = None
            for _round in range(MAX_TOOL_ROUNDS):
                message, call_usage = await self.llm.create(
                    model=self.settings.model_strong,
                    system=GENERATE_SYSTEM,
                    messages=messages,
                    tools=tool_defs,
                    output_schema=FINAL_SCHEMA,
                    effort=self.settings.llm_effort,
                )
                usage = usage.add(call_usage)
                messages.append(
                    {
                        "role": "assistant",
                        "content": [b.model_dump(exclude_none=True) for b in message.content],
                    }
                )

                tool_uses = [b for b in message.content if b.type == "tool_use"]
                if not tool_uses:
                    final = _parse_final(text_of(message))
                    break

                results: list[dict[str, Any]] = []
                for tu in tool_uses:
                    tool_calls.append(tu.name)
                    args = dict(tu.input) if isinstance(tu.input, dict) else {}
                    try:
                        out = await tools.call(tu.name, args)
                        if tu.name == "validate_sql":
                            validated = bool(out.get("valid"))
                        content = json.dumps(out, default=str)
                        results.append(
                            {"type": "tool_result", "tool_use_id": tu.id, "content": content}
                        )
                    except ToolCallError as e:
                        results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": tu.id,
                                "content": e.message,
                                "is_error": True,
                            }
                        )
                messages.append({"role": "user", "content": results})

                # Checkpoint: a retry resumes here instead of replaying earlier turns.
                ckpt = {
                    "messages": list(messages),  # snapshot; the list keeps growing
                    "tool_calls": list(tool_calls),
                    "usage": usage.model_dump(),
                    "validated": validated,
                }
                latest["ckpt"] = ckpt
                activity.heartbeat(ckpt)
                await self._maybe_crash("generate_sql", info.attempt, len(tool_calls))

        if final is None:
            raise RuntimeError(f"generate_sql did not finish within {MAX_TOOL_ROUNDS} tool rounds")
        return GeneratedSQL(
            sql=final["sql"].strip(),
            explanation=final["explanation"],
            self_reported_risk=final["self_reported_risk"],
            tool_calls=tool_calls,
            validated=validated,
            usage=usage,
            resumed_from_checkpoint=resumed,
        )

    # 4 ------------------------------------------------------------------------------------
    @activity.defn
    async def classify_risk(self, sql: str) -> RiskResult:
        """Deterministic. No LLM. The only risk signal the workflow acts on."""
        a = classify(sql, self.lakehouse.schema(), self.policy)
        return RiskResult(
            level=a.level.value,
            reasons=[f"{r.rule}: {r.message}" for r in a.reasons],
            sections=list(a.sections),
            tables=list(a.tables),
            limit=a.limit,
            scalar_aggregate=a.scalar_aggregate,
        )

    # 5 ------------------------------------------------------------------------------------
    @activity.defn
    async def execute_sql(self, sql: str) -> ExecutionResult:
        """Runs through the MCP server's run_sql with the capability token. Read-only, capped."""
        token = self.settings.run_sql_capability_token
        if token is None:
            raise RuntimeError("RUN_SQL_CAPABILITY_TOKEN is not configured on the worker")
        async with tool_client(
            self.settings.mcp_server_url, token=token.get_secret_value()
        ) as tools:
            out = await tools.call("run_sql", {"sql": sql})
        return ExecutionResult(
            columns=out["columns"],
            rows=out["rows"],
            row_count=out["row_count"],
            truncated=out["truncated"],
            elapsed_ms=out["elapsed_ms"],
        )

    # 6 ------------------------------------------------------------------------------------
    @activity.defn
    async def summarize(
        self,
        request: AnalystRequest,
        sql: str,
        result: ExecutionResult,
        citations: list[str],
        risk_level: str,
    ) -> tuple[Summary, LLMUsage]:
        summary, usage = await self.llm.structured(
            model=self.settings.model_fast,
            system=SUMMARIZE_SYSTEM,
            user=summarize_user(
                request.question,
                sql,
                result.columns,
                result.rows,
                result.truncated,
                citations,
                risk_level,
            ),
            schema=Summary,
        )
        return summary, usage

    # -- helpers ------------------------------------------------------------------------------

    async def _maybe_crash(self, stage: str, attempt: int, tool_calls_so_far: int) -> None:
        """Crash-recovery demo hook. SIGKILL is deliberate: no cleanup, like a real OOM kill.

        The sleep is ``await``-ed on purpose: the SDK encodes and sends the heartbeat on the
        event loop, so the loop must get a turn before the process dies or the checkpoint
        never leaves the worker.
        """
        if self.settings.demo_crash_at == stage and attempt == 1 and tool_calls_so_far >= 1:
            log.warning(
                "DEMO_CRASH_AT=%s: killing worker pid %d after %d tool call(s)",
                stage,
                os.getpid(),
                tool_calls_so_far,
            )
            await asyncio.sleep(1.5)  # let the heartbeat flush to the server before dying
            os.kill(os.getpid(), signal.SIGKILL)


def _parse_final(text: str) -> dict[str, Any]:
    """Structured output guarantees JSON; keep a lenient fallback for safety."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end < 0:
            raise ValueError(f"model did not return JSON: {text[:200]}") from None
        data = json.loads(text[start : end + 1])
    for key in ("sql", "explanation", "self_reported_risk"):
        data.setdefault(key, "")
    return dict(data)
