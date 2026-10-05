"""Crash-recovery demo: ``make demo-crash``.

What it does, in order:

1. Starts a worker with ``DEMO_CRASH_AT=generate_sql`` and a question that needs approval.
2. The worker SIGKILLs itself right after Claude's first tool call, with the conversation
   already checkpointed in heartbeat details on the Temporal server.
3. Shows, from the server alone (no worker alive), the pending activity, its attempt number,
   and the checkpoint it holds.
4. Starts a fresh worker without the crash flag. The server notices the missed heartbeats,
   times the activity attempt out, and retries it on the new worker, which resumes from the
   checkpoint instead of re-running the earlier Claude turn.
5. Auto-approves the run and prints the evidence: one ActivityTaskScheduled per step (no
   completed step re-ran), generate_sql started on attempt 2, ``resumed_from_checkpoint``,
   and the Claude call count.

Prerequisites: ``make temporal`` and ``make mcp`` running, no other worker on the task queue.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from temporalio.api.enums.v1 import EventType, TaskQueueType
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
from temporalio.client import Client, WorkflowHandle

from durable_agent.config import Settings, get_settings
from durable_agent.models import AnalystRequest, AnalystResult, ApprovalDecision, Stage
from durable_agent.worker import connect
from durable_agent.workflows import STAGE_KEY, AnalystWorkflow

QUESTION = "List the email addresses of enterprise-segment customers in Germany."
LOG_DIR = Path("data/demo")


def say(msg: str) -> None:
    print(msg, flush=True)


def _port_open(host: str, port: int) -> bool:
    try:
        socket.create_connection((host, port), timeout=0.3).close()
        return True
    except OSError:
        return False


def _spawn_worker(crash: bool, log_path: Path) -> subprocess.Popen[bytes]:
    env = {**os.environ}
    env.pop("DEMO_CRASH_AT", None)
    # The demo must prove recovery with heartbeat checkpoints alone, so the response cache is
    # off: every Claude call in the evidence is a real API call.
    env["LLM_CACHE"] = "false"
    if crash:
        env["DEMO_CRASH_AT"] = "generate_sql"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("wb")
    return subprocess.Popen(
        [sys.executable, "-m", "durable_agent.worker"], env=env, stdout=log, stderr=log
    )


def _wait_log(log_path: Path, needle: str, timeout_s: float = 90) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if log_path.exists() and needle in log_path.read_text(errors="replace"):
            return True
        time.sleep(0.2)
    return False


async def _pollers(client: Client, settings: Settings) -> int:
    resp = await client.workflow_service.describe_task_queue(
        DescribeTaskQueueRequest(
            namespace=settings.temporal_namespace,
            task_queue=TaskQueue(name=settings.temporal_task_queue),
            task_queue_type=TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY,
        )
    )
    return len(resp.pollers)


async def _stage(handle: WorkflowHandle[Any, Any]) -> str:
    """Stage from the search attribute: server-side, works with no worker alive."""
    desc = await handle.describe()
    return desc.typed_search_attributes.get(STAGE_KEY) or "?"


async def _pending_generate_sql(
    client: Client, handle: WorkflowHandle[Any, Any]
) -> dict[str, Any] | None:
    desc = await handle.describe()
    for pa in desc.raw_description.pending_activities:
        if pa.activity_type.name == "generate_sql":
            details: list[Any] = []
            if pa.heartbeat_details.payloads:
                details = await client.data_converter.decode(list(pa.heartbeat_details.payloads))
            ckpt = details[0] if details else None
            return {
                "attempt": pa.attempt,
                "tool_calls": (ckpt or {}).get("tool_calls", []),
                "messages": len((ckpt or {}).get("messages", [])),
                "last_failure": pa.last_failure.message if pa.HasField("last_failure") else "",
            }
    return None


async def _history_evidence(
    handle: WorkflowHandle[Any, Any],
) -> tuple[dict[str, int], dict[str, Any]]:
    scheduled: dict[str, int] = {}
    started: dict[int, Any] = {}
    async for ev in handle.fetch_history_events():
        if ev.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            name = ev.activity_task_scheduled_event_attributes.activity_type.name
            scheduled[name] = scheduled.get(name, 0) + 1
        elif ev.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_STARTED:
            a = ev.activity_task_started_event_attributes
            # map back to the scheduled event's activity type
            started[a.scheduled_event_id] = {
                "attempt": a.attempt,
                "last_failure": a.last_failure.message if a.HasField("last_failure") else "",
            }
    # second pass to name the started entries
    named: dict[str, Any] = {}
    async for ev in handle.fetch_history_events():
        if ev.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED and ev.event_id in started:
            named[ev.activity_task_scheduled_event_attributes.activity_type.name] = started[
                ev.event_id
            ]
    return scheduled, named


async def run_demo(settings: Settings) -> int:
    mcp = urlparse(settings.mcp_server_url)
    host, port = settings.temporal_address.rsplit(":", 1)
    if not _port_open(host, int(port)):
        say(f"Temporal is not reachable at {settings.temporal_address}.")
        say("Run `make temporal` in another terminal.")
        return 1
    if not _port_open(mcp.hostname or "127.0.0.1", mcp.port or 8765):
        say(f"MCP server is not reachable at {settings.mcp_server_url}.")
        say("Run `make mcp` in another terminal.")
        return 1
    client = await connect(settings)
    if n := await _pollers(client, settings):
        # A worker stopped in the last minute can still show up as a poller, so warn, don't abort.
        say(f"warning: {n} poller(s) on task queue '{settings.temporal_task_queue}'.")
        say("         if another `make worker` is really running, it may take the activity.")

    run_id = f"analyst-crash-{int(time.time())}"
    say("━━━ 1/5  start a worker that will crash mid-generate_sql (DEMO_CRASH_AT=generate_sql)")
    w1 = _spawn_worker(crash=True, log_path=LOG_DIR / "worker-1.log")
    if not _wait_log(LOG_DIR / "worker-1.log", "worker ready"):
        say("worker 1 did not start; see data/demo/worker-1.log")
        w1.kill()
        return 1
    say(f"     worker 1 pid={w1.pid} ready")

    say(f"━━━ 2/5  start workflow {run_id}")
    say(f'     question: "{QUESTION}"')
    handle = await client.start_workflow(
        AnalystWorkflow.run,
        AnalystRequest(question=QUESTION, requester="demo"),
        id=run_id,
        task_queue=settings.temporal_task_queue,
    )
    say(
        f"     ui: http://localhost:8233/namespaces/{settings.temporal_namespace}/workflows/{run_id}"
    )

    last = ""
    while w1.poll() is None:
        stage = await _stage(handle)
        if stage != last and stage != "?":
            say(f"     stage: {stage}")
            last = stage
        if stage in {s.value for s in (Stage.COMPLETED, Stage.BLOCKED, Stage.FAILED)}:
            say("workflow finished before the crash hook fired; nothing to demonstrate")
            w1.kill()
            return 1
        await asyncio.sleep(0.5)
    say(f"💥   worker 1 died (pid={w1.pid}, exit={w1.returncode}) with the activity in flight")

    say("━━━ 3/5  what the Temporal server knows, with NO worker alive")
    pending = await _pending_generate_sql(client, handle)
    if pending is None:
        say("     no pending generate_sql activity found (unexpected)")
        return 1
    say(f"     workflow stage (search attribute): {await _stage(handle)}")
    say(f"     pending activity: generate_sql attempt={pending['attempt']}")
    n_calls, n_msgs = len(pending["tool_calls"]), pending["messages"]
    say(f"     checkpoint in heartbeat details: {n_calls} tool call(s) {pending['tool_calls']}")
    say(f"     checkpoint conversation length: {n_msgs} messages")
    say("     completed activities (retrieve_context, plan_query) are in history: no re-run")

    say("━━━ 4/5  start a fresh worker (no crash flag) and let the server retry the activity")
    w2 = _spawn_worker(crash=False, log_path=LOG_DIR / "worker-2.log")
    if not _wait_log(LOG_DIR / "worker-2.log", "worker ready"):
        say("worker 2 did not start; see data/demo/worker-2.log")
        w2.kill()
        return 1
    say(f"     worker 2 pid={w2.pid} ready; waiting for the heartbeat timeout to expire ...")
    t0 = time.time()
    while True:
        p = await _pending_generate_sql(client, handle)
        if p is None or p["attempt"] >= 2:
            break
        await asyncio.sleep(0.5)
    say(
        f"     server retried generate_sql after {time.time() - t0:.1f}s"
        + (
            f" (attempt={p['attempt']}, cause: {p['last_failure'] or 'heartbeat timeout'})"
            if p
            else ""
        )
    )
    if _wait_log(LOG_DIR / "worker-2.log", "resumed from checkpoint", timeout_s=120):
        say("     worker 2 log: generate_sql resumed from checkpoint")

    while (stage := await _stage(handle)) not in {  # noqa: ASYNC110 - polling a remote server
        Stage.AWAITING_APPROVAL.value,
        Stage.COMPLETED.value,
        Stage.BLOCKED.value,
        Stage.FAILED.value,
        Stage.REJECTED.value,
    }:
        await asyncio.sleep(0.5)
    say(f"     stage: {stage}")
    if stage == Stage.AWAITING_APPROVAL.value:
        say("━━━ 5/5  approve (this is where a human would decide; the demo auto-approves)")
        await handle.signal(
            AnalystWorkflow.approve,
            ApprovalDecision(approved=True, note="demo auto-approval", decided_by="demo"),
        )
    result: AnalystResult = await handle.result()
    say(f"     stage: {result.stage.value}")

    scheduled, started = await _history_evidence(handle)
    say("\n━━━ evidence from the workflow history")
    for name, n in scheduled.items():
        extra = f"  attempt={started[name]['attempt']}" if name in started else ""
        if name in started and started[name]["last_failure"]:
            extra += f"  previous attempt failed: {started[name]['last_failure']}"
        say(f"     {name:<18} scheduled {n}x{extra}")
    say(f"     resumed_from_checkpoint = {result.resumed_from_checkpoint}")
    say(f"     tool calls              = {' -> '.join(result.tool_calls)}")
    say(f"     Claude calls            = {result.usage.calls} real API calls across both attempts")
    say("                               (plan + generate_sql turns incl. pre-crash + summary)")
    say(f"     estimated cost          = ${result.usage.cost_usd:.4f}  (response cache off)")
    say(f"     rows returned           = {result.result.row_count if result.result else 0}")

    w2.terminate()
    try:
        w2.wait(timeout=10)
    except subprocess.TimeoutExpired:
        w2.kill()
    say("\nworker 2 stopped. Open the run in the Temporal UI to see the same timeline.")
    return 0


def main() -> int:
    return asyncio.run(run_demo(get_settings()))


if __name__ == "__main__":
    sys.exit(main())
