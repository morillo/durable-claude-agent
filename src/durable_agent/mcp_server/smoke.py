"""One-command smoke test: start the MCP server, exercise every tool, stop the server.

``make smoke-mcp`` runs this. It needs the seed data and the index to exist
(``make seed`` and ``make index``); nothing else. No API tokens are spent.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from durable_agent.mcp_server.client import ToolCallError, tool_client

URL = "http://127.0.0.1:8765/mcp"
TOKEN = "smoke-test-token"  # noqa: S105 - local throwaway value, never used elsewhere
SQL = "SELECT status, count(*) AS n FROM orders GROUP BY 1 ORDER BY 2 DESC LIMIT 10"


def _wait_for_port(host: str, port: int, timeout_s: float = 30) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            socket.create_connection((host, port), timeout=0.2).close()
            return True
        except OSError:
            time.sleep(0.1)
    return False


async def _checks() -> None:
    async with tool_client(URL) as c:
        print("1. tools exposed by the server:", ", ".join(await c.tool_names()))
        v = await c.call("validate_sql", {"sql": SQL})
        print(f"2. validate_sql -> valid={v['valid']} risk={v['risk']['level']}")
        s = await c.call("search_governance", {"query": "how is net revenue defined", "k": 1})
        print(f"3. search_governance -> top citation {s['passages'][0]['citation']}")
        try:
            await c.call("run_sql", {"sql": SQL})
            print("4. run_sql WITHOUT token -> executed (THIS IS A BUG)")
        except ToolCallError as e:
            print(f"4. run_sql WITHOUT token -> refused: {e.message}")
    async with tool_client(URL, token=TOKEN) as c:
        r = await c.call("run_sql", {"sql": SQL})
        print(f"5. run_sql WITH token -> {r['row_count']} rows: {r['rows']}")
        try:
            await c.call("run_sql", {"sql": "SELECT * FROM payment_cards"})
            print("6. run_sql on restricted table -> executed (THIS IS A BUG)")
        except ToolCallError as e:
            print(f"6. run_sql on restricted table WITH token -> refused: {e.message}")


def main() -> int:
    for needed, cmd in [
        (Path("data/lakehouse/MANIFEST.json"), "make seed"),
        (Path("data/index/meta.json"), "make index"),
    ]:
        if not needed.exists():
            print(f"missing {needed}. Run `{cmd}` first.")
            return 1
    env = {**os.environ, "RUN_SQL_CAPABILITY_TOKEN": TOKEN, "MCP_SERVER_URL": URL}
    print(f"starting MCP server on {URL} ...")
    server = subprocess.Popen(
        [sys.executable, "-m", "durable_agent.mcp_server"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        if not _wait_for_port("127.0.0.1", 8765):
            print("server did not start within 30s (is port 8765 in use?)")
            return 1
        asyncio.run(_checks())
        print("\nsmoke test passed")
        return 0
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
        print("server stopped")


if __name__ == "__main__":
    sys.exit(main())
