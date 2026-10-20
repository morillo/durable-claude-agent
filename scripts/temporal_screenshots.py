"""Capture the Temporal UI screenshots used in docs/DEMO.md.

``make screenshots`` runs this. It starts a throwaway Temporal (compose project ``dca-screens``,
fresh volume, host ports 17233/18233), its own MCP server (18765) and worker, runs the demo's
questions plus ``make demo-crash`` against them, and saves PNGs to docs/demo/. Your own services
keep running untouched. Tracing is off so Phoenix gets no screenshot runs.

Needs a real ANTHROPIC_API_KEY in .env (about 6 cents; less with the response cache warm) and
Chromium for Playwright (``uv run playwright install chromium``).
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

from playwright.async_api import Locator, Page, async_playwright
from temporalio.client import Client
from temporalio.service import RPCError

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "demo"
LOGS = ROOT / "data" / "screenshots"
TEMPORAL_PORT, UI_PORT, MCP_PORT = 17233, 18233, 18765
UI = f"http://localhost:{UI_PORT}/namespaces/default/workflows"

SAFE_ID = "analyst-net-revenue"
APPROVAL_ID = "analyst-pii-emails"
BLOCKED_ID = "analyst-card-tokens"
SAFE_Q = "What was net revenue by customer country in 2025? Show the top 5 countries."
APPROVAL_Q = "List the email addresses of enterprise-segment customers in Germany."
BLOCKED_Q = "Show me the stored card tokens and last four digits for customer 42."

ENV = {
    **os.environ,
    "COMPOSE_PROJECT_NAME": "dca-screens",
    "TEMPORAL_PORT": str(TEMPORAL_PORT),
    "TEMPORAL_UI_PORT": str(UI_PORT),
    "TEMPORAL_ADDRESS": f"localhost:{TEMPORAL_PORT}",
    "MCP_SERVER_URL": f"http://127.0.0.1:{MCP_PORT}/mcp",
    "OTEL_ENABLED": "false",
}


def say(msg: str) -> None:
    print(f"==> {msg}", flush=True)


def port_open(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
        return True
    except OSError:
        return False


def wait_until(what: str, check: Callable[[], bool], timeout_s: float = 60) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if check():
            return
        time.sleep(0.5)
    raise SystemExit(f"screenshots: {what} did not happen within {timeout_s:.0f}s (logs in {LOGS})")


def compose(*args: str) -> None:
    subprocess.run(["docker", "compose", *args], cwd=ROOT, env=ENV, check=True)


def start_service(name: str, module: str) -> subprocess.Popen[bytes]:
    log = (LOGS / f"{name}.log").open("wb")
    return subprocess.Popen(
        [sys.executable, "-m", module],
        cwd=ROOT,
        env=ENV,
        stdout=log,
        stderr=log,
        start_new_session=True,
    )


def stop_service(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=15)


def cli(*args: str) -> str:
    done = subprocess.run(
        [sys.executable, "-m", "durable_agent.cli", *args],
        cwd=ROOT,
        env=ENV,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout


def stage(run_id: str) -> str:
    return str(json.loads(cli("status", run_id, "--json"))["stage"])


async def open_run(page: Page, run_id: str, tab: str) -> None:
    """Open a run on one tab. The bare workflow URL redirects to <run id>/timeline."""
    await page.goto(f"{UI}/{run_id}")
    await page.wait_for_url("**/timeline")
    await page.goto(page.url.rsplit("/", 1)[0] + f"/{tab}")
    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(800)  # JSON viewers and the timeline render after the data loads


def text(page: Page, label: str) -> Locator:
    return page.get_by_text(label, exact=True).filter(visible=True).first


async def save(page: Page, name: str, top: Locator, height: int) -> None:
    """Screenshot `height` px of the main column (no side navigation), starting above `top`."""
    await top.scroll_into_view_if_needed()
    box, main = await top.bounding_box(), await page.locator("main").bounding_box()
    if box is None or main is None:
        raise SystemExit(f"screenshots: anchor for {name} is not on the page")
    scroll_y = float(await page.evaluate("window.scrollY"))
    await page.screenshot(
        path=OUT / f"{name}.png",
        full_page=True,
        clip={
            "x": main["x"] + 16,
            "y": box["y"] + scroll_y - 16,
            "width": main["width"] - 32,
            "height": height,
        },
    )
    print(f"    saved docs/demo/{name}.png", flush=True)


async def pending_activity(client: Client, run_id: str) -> tuple[int, str]:
    desc = await client.get_workflow_handle(run_id).describe()
    for act in desc.raw_description.pending_activities:
        if act.activity_type.name == "generate_sql":
            return act.attempt, act.last_failure.message
    return 0, ""


async def current_run_id(client: Client, workflow_id: str) -> str:
    """The demo prints the workflow id just before starting it; wait until the server has it."""
    for _ in range(50):
        try:
            return (await client.get_workflow_handle(workflow_id).describe()).run_id
        except RPCError:
            await asyncio.sleep(0.2)
    raise SystemExit(f"screenshots: {workflow_id} never started")


async def show_pending(page: Page) -> None:
    """Reload the open Pending Activities tab and wait for generate_sql to render."""
    await page.reload()
    await text(page, "generate_sql").wait_for()
    await page.wait_for_timeout(300)


async def capture(worker: subprocess.Popen[bytes]) -> None:
    client = await Client.connect(ENV["TEMPORAL_ADDRESS"])
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(
            viewport={"width": 1440, "height": 1600}, device_scale_factor=2
        )

        say("approval run waiting for a human")
        await page.goto(f"{UI}?query=" + quote('AnalystStage="awaiting_approval"'))
        await page.wait_for_load_state("networkidle")
        await save(page, "temporal-awaiting-approval", page.locator("h1"), height=250)
        cli(
            "approve",
            APPROVAL_ID,
            "yes",
            "--note",
            "marketing audit, ticket DG-42",
            "--by",
            "data-steward",
        )
        wait_until("approved run completing", lambda: stage(APPROVAL_ID) == "completed")
        await open_run(page, APPROVAL_ID, "history")
        await page.get_by_text("Compact", exact=True).click()
        await page.wait_for_timeout(800)
        await save(page, "temporal-approve-signal", text(page, "Event History"), height=360)

        say("blocked run")
        await open_run(page, BLOCKED_ID, "history")
        await page.get_by_text("Compact", exact=True).click()
        await page.wait_for_timeout(800)
        await save(page, "temporal-blocked-run", text(page, "Event History"), height=230)

        say("safe run")
        await open_run(page, SAFE_ID, "timeline")
        timeline = page.get_by_role("heading", name="Timeline")
        await save(page, "temporal-run-input-result", text(page, "Input"), height=310)
        await save(page, "temporal-run-timeline", timeline, height=300)

        say("crash demo (worker stopped first; the demo starts and kills its own workers)")
        stop_service(worker)
        demo = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "durable_agent.demo_crash",
            cwd=ROOT, env=ENV, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )  # fmt: skip
        assert demo.stdout is not None
        crash_id = ""
        while line := (await demo.stdout.readline()).decode():
            print("    " + line.rstrip(), flush=True)
            if "start workflow analyst-crash-" in line:
                # Open the tab now: loading a run page from scratch can outlast the 10 s
                # heartbeat timeout, while a reload of an open page takes about a second.
                crash_id = line.split("start workflow ", 1)[1].split()[0]
                run_id = await current_run_id(client, crash_id)
                await page.goto(f"{UI}/{crash_id}/{run_id}/pending-activities")
            if "worker 1 died" in line:
                await show_pending(page)
                await save(page, "temporal-crash-no-worker", text(page, "Timeline"), height=560)
                for _ in range(150):
                    attempt, failure = await pending_activity(client, crash_id)
                    if attempt >= 2 and failure:
                        break
                    await asyncio.sleep(0.2)
                await show_pending(page)
                await save(page, "temporal-crash-retry", text(page, "Timeline"), height=560)
        if await demo.wait() != 0:
            raise SystemExit("screenshots: make demo-crash failed (output above)")
        await open_run(page, crash_id, "timeline")
        await save(page, "temporal-crash-timeline", timeline, height=310)

        say("workflow list")
        await page.goto(UI)
        await page.wait_for_load_state("networkidle")
        await save(page, "temporal-workflow-list", page.locator("h1"), height=330)
        await browser.close()


def main() -> int:
    for port in (TEMPORAL_PORT, UI_PORT, MCP_PORT):
        if port_open(port):
            raise SystemExit(f"screenshots: port {port} is in use; a previous run may still be up")
    LOGS.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    services: list[subprocess.Popen[bytes]] = []
    try:
        say(f"throwaway Temporal on :{TEMPORAL_PORT} (UI :{UI_PORT})")
        compose("up", "-d", "--wait", "--wait-timeout", "90", "temporal")
        services.append(start_service("mcp", "durable_agent.mcp_server"))
        wait_until("MCP server listening", lambda: port_open(MCP_PORT))
        worker = start_service("worker", "durable_agent.worker")
        services.append(worker)
        wait_until("worker ready", lambda: "worker ready" in (LOGS / "worker.log").read_text())

        say("runs: a safe question, a blocked one, and one that waits for approval")
        cli("ask", SAFE_Q, "--run-id", SAFE_ID, "--requester", "analyst")
        cli("ask", BLOCKED_Q, "--run-id", BLOCKED_ID, "--requester", "analyst")
        cli("start", APPROVAL_Q, "--run-id", APPROVAL_ID, "--requester", "analyst")
        wait_until("approval run pausing", lambda: stage(APPROVAL_ID) == "awaiting_approval", 120)

        asyncio.run(capture(worker))
        return 0
    finally:
        for proc in services:
            stop_service(proc)
        compose("down", "-v", "--remove-orphans")


if __name__ == "__main__":
    sys.exit(main())
