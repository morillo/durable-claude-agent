"""Command-line entrypoint: ``durable-agent ask|start|approve|status``.

``ask`` starts a workflow and streams its stage until it finishes, printing a human-readable
report. ``start`` returns immediately with the run id; ``status`` queries live state;
``approve`` sends the approval signal. All of these talk only to Temporal, never to the
worker directly, which is the point: the worker can be anywhere, or dead, and the CLI works.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

from temporalio.client import Client, WorkflowHandle

from durable_agent import __version__
from durable_agent.config import Settings, get_settings
from durable_agent.models import AnalystRequest, AnalystResult, ApprovalDecision, Stage
from durable_agent.worker import connect
from durable_agent.workflows import AnalystWorkflow

TERMINAL = {
    Stage.COMPLETED,
    Stage.BLOCKED,
    Stage.REJECTED,
    Stage.APPROVAL_TIMEOUT,
    Stage.FAILED,
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="durable-agent", description="Claude text-to-SQL analyst on Temporal."
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    ask = sub.add_parser("ask", help="Start a run and wait for it, printing progress.")
    ask.add_argument("question")
    ask.add_argument("--requester", default="cli-user")
    ask.add_argument("--run-id", default=None, help="Workflow id (default: analyst-<short uuid>).")
    ask.add_argument(
        "--approval-timeout", type=int, default=24 * 3600, help="Seconds to wait for approval."
    )
    ask.add_argument("--json", action="store_true", help="Print the final result as JSON.")

    start = sub.add_parser("start", help="Start a run and return immediately.")
    start.add_argument("question")
    start.add_argument("--requester", default="cli-user")
    start.add_argument("--run-id", default=None)
    start.add_argument("--approval-timeout", type=int, default=24 * 3600)

    approve = sub.add_parser("approve", help="Approve or reject a run awaiting approval.")
    approve.add_argument("run_id")
    approve.add_argument("decision", choices=["yes", "no"])
    approve.add_argument("--note", default="")
    approve.add_argument("--by", default="cli-approver")

    status = sub.add_parser("status", help="Print the live state of a run.")
    status.add_argument("run_id")
    status.add_argument("--json", action="store_true")
    return p


def _handle(client: Client, run_id: str) -> WorkflowHandle[AnalystWorkflow, AnalystResult]:
    return client.get_workflow_handle_for(AnalystWorkflow.run, run_id)


async def _start(
    settings: Settings, args: argparse.Namespace
) -> tuple[Client, WorkflowHandle[AnalystWorkflow, AnalystResult]]:
    client = await connect(settings)
    run_id = args.run_id or f"analyst-{uuid.uuid4().hex[:8]}"
    handle = await client.start_workflow(
        AnalystWorkflow.run,
        AnalystRequest(
            question=args.question,
            requester=args.requester,
            approval_timeout_seconds=args.approval_timeout,
        ),
        id=run_id,
        task_queue=settings.temporal_task_queue,
    )
    ui = f"http://localhost:8233/namespaces/{settings.temporal_namespace}/workflows/{run_id}"
    print(f"started {run_id}\n  ui: {ui}")
    return client, handle


async def cmd_start(settings: Settings, args: argparse.Namespace) -> int:
    await _start(settings, args)
    return 0


async def cmd_ask(settings: Settings, args: argparse.Namespace) -> int:
    _, handle = await _start(settings, args)
    last = None
    while True:
        try:
            state = await handle.query(AnalystWorkflow.state)
        except Exception:  # query can race the first workflow task
            await asyncio.sleep(0.5)
            continue
        if state.stage != last:
            print(f"  stage: {state.stage.value}")
            if state.stage == Stage.AWAITING_APPROVAL:
                reasons = "; ".join(state.risk.reasons) if state.risk else ""
                print(f"  risk : {state.risk.level if state.risk else '?'} -> {reasons}")
                print(f"  sql  : {state.sql}")
                print(f"  approve with: make approve RUN_ID={handle.id} DECISION=yes")
            last = state.stage
        if state.stage in TERMINAL:
            break
        await asyncio.sleep(1.0)
    try:
        result = await handle.result()
    except Exception as e:  # surfaced below with exit code 1
        print(f"\nworkflow failed: {e}")
        return 1
    print_report(result, as_json=args.json)
    # blocked / rejected / timed out are the system working as designed: exit 0.
    return 0 if result.stage != Stage.FAILED else 1


async def cmd_approve(settings: Settings, args: argparse.Namespace) -> int:
    client = await connect(settings)
    handle = _handle(client, args.run_id)
    await handle.signal(
        AnalystWorkflow.approve,
        ApprovalDecision(approved=args.decision == "yes", note=args.note, decided_by=args.by),
    )
    print(f"sent decision={args.decision} to {args.run_id}")
    return 0


async def cmd_status(settings: Settings, args: argparse.Namespace) -> int:
    client = await connect(settings)
    state = await _handle(client, args.run_id).query(AnalystWorkflow.state)
    print_report(state, as_json=args.json)
    return 0


def print_report(r: AnalystResult, *, as_json: bool = False) -> None:
    if as_json:
        print(r.model_dump_json(indent=2))
        return
    print(f"\n== {r.stage.value.upper()} ==")
    print(f"question : {r.question}")
    if r.sql:
        print(f"sql      : {r.sql}")
    if r.explanation:
        print(f"why      : {r.explanation}")
    if r.risk:
        print(
            f"risk     : {r.risk.level}"
            + (f" ({'; '.join(r.risk.reasons)})" if r.risk.reasons else "")
        )
    if r.approval:
        verdict = "approved" if r.approval.approved else "rejected"
        print(f"approval : {verdict} by {r.approval.decided_by} {r.approval.note!r}")
    if r.summary:
        print(f"\n{r.summary.answer}")
        for c in r.summary.caveats:
            print(f"  - {c}")
    if r.result:
        trunc = " (truncated)" if r.result.truncated else ""
        print(f"\nrows     : {r.result.row_count}{trunc} in {r.result.elapsed_ms} ms")
        if r.result.rows:
            print(f"columns  : {r.result.columns}")
            for row in r.result.rows[:10]:
                print(f"           {row}")
    if r.citations:
        print(f"cites    : {', '.join(r.citations)}")
    if r.error:
        print(f"note     : {r.error}")
    u = r.usage
    print(
        f"tokens   : in={u.input_tokens} out={u.output_tokens} cache_read={u.cache_read_tokens} "
        f"calls={u.calls} cached={u.cached_responses} cost=${u.cost_usd:.4f}"
    )
    if r.tool_calls:
        print(
            f"tools    : {' -> '.join(r.tool_calls)}"
            + ("  [resumed from checkpoint]" if r.resumed_from_checkpoint else "")
        )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    cmd = {"ask": cmd_ask, "start": cmd_start, "approve": cmd_approve, "status": cmd_status}[
        args.cmd
    ]
    return asyncio.run(cmd(settings, args))


if __name__ == "__main__":
    sys.exit(main())
