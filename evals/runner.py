"""Run the dataset through the real AnalystWorkflow and score it.

Why through the workflow and not the LLM calls in isolation: orchestration bugs (a gate that
lets SQL run before approval, a retry that double-executes, a serialization error in a result
model) are exactly the failures an enterprise cares about, and a function-level eval cannot see
them. So the runner boots a private Temporal dev server, an in-process MCP server, and a real
worker with the real activities, then starts one workflow per question.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import secrets
import shutil
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

import uvicorn
from pydantic import SecretStr
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client, WorkflowHandle
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from durable_agent.activities import AnalystActivities
from durable_agent.config import Settings, get_settings
from durable_agent.lakehouse import Lakehouse
from durable_agent.llm import LLM
from durable_agent.mcp_server.server import AppState, create_server
from durable_agent.models import AnalystRequest, AnalystResult, ApprovalDecision, LLMUsage, Stage
from durable_agent.retrieval import GovernanceIndex, SentenceTransformerEmbedder
from durable_agent.risk import load_policy
from durable_agent.workflows import SEARCH_ATTRIBUTES, STAGE_KEY, AnalystWorkflow, workflow_runner
from evals.dataset import (
    DATASET_PATH,
    Example,
    gold_hashes,
    load_dataset,
    write_gold_hashes,
)
from evals.judge import judge_tool_use
from evals.scorecard import aggregate, write_outputs
from evals.scorers import approval_behavior, execution_accuracy, retrieval_recall, risk_accuracy

log = logging.getLogger("evals")
TERMINAL = {Stage.COMPLETED, Stage.BLOCKED, Stage.REJECTED, Stage.APPROVAL_TIMEOUT, Stage.FAILED}
TASK_QUEUE = "eval-analyst"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class McpInProcess:
    """The MCP server on a uvicorn thread, so the eval is one process plus the dev server."""

    def __init__(self, state: AppState) -> None:
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        app = create_server(state).streamable_http_app(streamable_http_path="/mcp")
        self._server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning")
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def start(self) -> None:
        self._thread.start()
        for _ in range(200):
            if self._server.started:
                return
            time.sleep(0.05)
        raise RuntimeError("in-process MCP server did not start")

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)


async def _history_facts(handle: WorkflowHandle[Any, Any]) -> tuple[bool, bool]:
    """(executed, executed_before_decision) from the event history."""
    signal_at: int | None = None
    exec_at: int | None = None
    async for ev in handle.fetch_history_events():
        if ev.event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED and signal_at is None:
            signal_at = ev.event_id
        if (
            ev.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
            and ev.activity_task_scheduled_event_attributes.activity_type.name == "execute_sql"
            and exec_at is None
        ):
            exec_at = ev.event_id
    executed = exec_at is not None
    before = executed and (signal_at is None or exec_at < signal_at)  # type: ignore[operator]
    return executed, before


async def run_one(
    client: Client, ex: Example, run_tag: str, task_queue: str
) -> tuple[AnalystResult, list[str], bool, bool, float]:
    t0 = time.perf_counter()
    handle = await client.start_workflow(
        AnalystWorkflow.run,
        AnalystRequest(question=ex.question, requester="eval", approval_timeout_seconds=600),
        id=f"eval-{run_tag}-{ex.id}",
        task_queue=task_queue,
    )
    stages: list[str] = []
    approved = False
    while True:
        desc = await handle.describe()
        stage = desc.typed_search_attributes.get(STAGE_KEY) or ""
        if stage and (not stages or stages[-1] != stage):
            stages.append(stage)
        if stage == Stage.AWAITING_APPROVAL.value and not approved:
            await handle.signal(
                AnalystWorkflow.approve,
                ApprovalDecision(approved=True, note="eval auto-approval", decided_by="eval"),
            )
            approved = True
        if stage in {s.value for s in TERMINAL}:
            break
        await asyncio.sleep(0.5)
    try:
        result: AnalystResult = await handle.result()
    except Exception as e:  # workflow failure is a scored outcome, not a harness crash
        result = AnalystResult(
            stage=Stage.FAILED, question=ex.question, requester="eval", error=str(e)[:300]
        )
    executed, before = await _history_facts(handle)
    return result, stages, executed, before, time.perf_counter() - t0


async def run_eval(
    settings: Settings, examples: list[Example], concurrency: int, out_dir: Path
) -> dict[str, Any]:
    lakehouse = Lakehouse(settings.lakehouse_path)
    policy = load_policy(settings.governance_path / "policies.md")
    embedder = SentenceTransformerEmbedder(settings.embedding_model)
    index = GovernanceIndex(settings.index_path, embedder)

    # dataset integrity: gold hashes must match this lakehouse
    hashes = gold_hashes(lakehouse, examples)
    stale = [
        ex.id for ex in examples if ex.gold_rows_hash and ex.gold_rows_hash != hashes.get(ex.id)
    ]
    if stale:
        raise SystemExit(
            f"gold_rows_hash mismatch for {stale}: seed data or gold SQL changed; run `make eval-gold`"
        )

    token = (
        settings.run_sql_capability_token.get_secret_value()
        if settings.run_sql_capability_token
        else secrets.token_urlsafe(16)
    )
    state = AppState(settings=settings, lakehouse=lakehouse, policy=policy, index=index)
    mcp = McpInProcess(state)
    mcp.start()
    eval_settings = settings.model_copy(
        update={
            "mcp_server_url": mcp.url,
            "run_sql_capability_token": SecretStr(token),
            "temporal_task_queue": TASK_QUEUE,
        }
    )
    state.settings = eval_settings
    llm = LLM(eval_settings)
    acts = AnalystActivities(
        settings=eval_settings, llm=llm, lakehouse=lakehouse, policy=policy, index=index
    )
    run_tag = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    records: list[dict[str, Any]] = []

    env = await WorkflowEnvironment.start_local(
        data_converter=pydantic_data_converter,
        search_attributes=SEARCH_ATTRIBUTES,
        dev_server_existing_path=shutil.which("temporal"),
        dev_server_log_level="error",
    )
    try:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[AnalystWorkflow],
            activities=acts.all,
            workflow_runner=workflow_runner(),
            default_heartbeat_throttle_interval=dt.timedelta(seconds=1),
            max_heartbeat_throttle_interval=dt.timedelta(seconds=2),
        ):
            sem = asyncio.Semaphore(concurrency)

            async def one(ex: Example) -> dict[str, Any]:
                async with sem:
                    result, stages, executed, before, wall = await run_one(
                        env.client, ex, run_tag, TASK_QUEUE
                    )
                risk_level = result.risk.level if result.risk else None
                execution = (
                    execution_accuracy(lakehouse, ex.gold_sql, result.sql)
                    if result.stage != Stage.FAILED
                    else {
                        "applicable": ex.gold_sql is not None,
                        "match": False,
                        "error": "workflow failed",
                    }
                )
                judge: dict[str, Any] | None = None
                judge_usage: dict[str, Any] | None = None
                if result.stage != Stage.FAILED:
                    verdict, ju = await judge_tool_use(
                        llm,
                        eval_settings.model_strong,
                        question=ex.question,
                        tool_calls=result.tool_calls,
                        sql=result.sql,
                        validated=result.sql_validated,
                        risk=risk_level or "n/a",
                        supplied_sections=result.retrieved_citations,
                    )
                    judge, judge_usage = verdict.model_dump(), ju.model_dump()
                rec: dict[str, Any] = {
                    "id": ex.id,
                    "tier": ex.tier,
                    "question": ex.question,
                    "expected_risk": ex.expected_risk,
                    "stage": result.stage.value,
                    "stages": stages,
                    "sql": result.sql,
                    "gold_sql": ex.gold_sql,
                    "error": result.error,
                    "tool_calls": result.tool_calls,
                    "execution": execution,
                    "risk": risk_accuracy(ex.expected_risk, risk_level),
                    "retrieval": retrieval_recall(ex.expected_sections, result.retrieved_citations),
                    "judge": judge,
                    "judge_usage": judge_usage,
                    "approval": approval_behavior(ex.expected_risk, stages, executed, before),
                    "usage": (result.usage or LLMUsage(model="none")).model_dump(),
                    "wall_seconds": round(wall, 1),
                    "resumed_from_checkpoint": result.resumed_from_checkpoint,
                    "sql_validated": result.sql_validated,
                }
                marks = ("✓" if execution["match"] else "✗" if execution["applicable"] else "·") + (
                    "✓" if rec["risk"]["match"] else "✗"
                )
                log.info(
                    "%s %-6s %-17s stage=%-17s risk=%s %5.1fs $%.4f",
                    marks,
                    ex.id,
                    ex.tier,
                    result.stage.value,
                    risk_level,
                    wall,
                    result.usage.cost_usd,
                )
                return rec

            records = list(await asyncio.gather(*(one(ex) for ex in examples)))
    finally:
        await env.shutdown()
        mcp.stop()

    records.sort(key=lambda r: r["id"])
    config = {
        "timestamp": run_tag,
        "model_fast": settings.model_fast,
        "model_strong": settings.model_strong,
        "llm_effort": settings.llm_effort,
        "llm_cache": settings.llm_cache,
        "embedding_model": settings.embedding_model,
        "concurrency": concurrency,
        "dataset": str(DATASET_PATH.name),
        "n": len(examples),
    }
    card = aggregate(records, config)
    write_outputs(out_dir / run_tag, records, card)
    card["out_dir"] = str(out_dir / run_tag)
    return card


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate the analyst workflow on evals/dataset.jsonl"
    )
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--limit", type=int, default=None, help="Only the first N examples.")
    parser.add_argument("--ids", default=None, help="Comma-separated example ids to run.")
    parser.add_argument("--out", type=Path, default=Path("evals/results"))
    parser.add_argument(
        "--write-gold",
        action="store_true",
        help="Recompute gold_rows_hash in the dataset and exit.",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx2", "httpx", "temporalio", "mcp", "sentence_transformers", "uvicorn"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    settings = get_settings()
    examples = load_dataset()
    if args.write_gold:
        hashes = gold_hashes(Lakehouse(settings.lakehouse_path), examples)
        write_gold_hashes(DATASET_PATH, hashes)
        print(f"wrote gold_rows_hash for {len(hashes)} examples")
        return 0
    if args.ids:
        wanted = set(args.ids.split(","))
        examples = [e for e in examples if e.id in wanted]
    if args.limit:
        examples = examples[: args.limit]
    if settings.anthropic_api_key is None:
        print("ANTHROPIC_API_KEY is not set (evals call Claude).")
        return 1
    card = asyncio.run(run_eval(settings, examples, args.concurrency, args.out))
    m = card["metrics"]
    print(
        f"\nexecution={m['execution_accuracy']} risk={m['risk_accuracy']} recall={m['retrieval_recall_at_k']} "
        f"judge={m['tool_use_judge']} approval={m['approval_behavior']} total=${card['cost']['total_usd']}"
    )
    print(f"scorecard: {card['out_dir']}/scorecard.md")
    healthy = m["completed_without_workflow_failure"] == 1.0
    if not healthy and not args.allow_workflow_failures:
        print("FAIL: at least one workflow failed (orchestration health < 100%)")
        return 1
    if (m["execution_accuracy"] or 0.0) < args.min_execution:
        print(
            f"FAIL: execution accuracy {m['execution_accuracy']} below --min-execution {args.min_execution}"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
