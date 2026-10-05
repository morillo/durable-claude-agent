"""The crash hook fires only under the demo conditions, and generate_sql resumes from a checkpoint.

The resume test runs the real activity against the real MCP server over HTTP with a scripted
Anthropic client: given heartbeat details that already contain one tool round, the activity
must make exactly one more Claude call (the final answer) and never replay the first turn.
"""

from __future__ import annotations

import dataclasses
import os
import signal
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from anthropic.types.beta import BetaMessage
from temporalio.testing import ActivityEnvironment

from durable_agent.activities import AnalystActivities
from durable_agent.config import Settings
from durable_agent.llm import LLM
from durable_agent.mcp_server import create_server
from durable_agent.mcp_server.server import AppState
from durable_agent.models import AnalystRequest, Passage, QueryPlan, RetrievedContext
from durable_agent.retrieval import HashEmbedder, build_index
from tests.conftest import GOVERNANCE_DIR

# ---------------------------------------------------------------------------------------------
# crash hook
# ---------------------------------------------------------------------------------------------


def _acts(settings: Settings) -> AnalystActivities:
    # Only _maybe_crash is exercised; heavy deps are not needed.
    return AnalystActivities(settings=settings, llm=None, lakehouse=None, policy=None, index=None)  # type: ignore[arg-type]


async def _no_sleep(_: float) -> None:
    return None


async def test_crash_hook_inert_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr("durable_agent.activities.asyncio.sleep", _no_sleep)
    acts = _acts(Settings(_env_file=None, demo_crash_at=None))
    await acts._maybe_crash("generate_sql", attempt=1, tool_calls_so_far=3)
    assert killed == []


async def test_crash_hook_fires_once_on_first_attempt_after_first_tool_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr("durable_agent.activities.asyncio.sleep", _no_sleep)
    acts = _acts(Settings(_env_file=None, demo_crash_at="generate_sql"))
    await acts._maybe_crash("generate_sql", attempt=1, tool_calls_so_far=0)  # no tool call yet
    await acts._maybe_crash("plan_query", attempt=1, tool_calls_so_far=1)  # wrong stage
    await acts._maybe_crash("generate_sql", attempt=2, tool_calls_so_far=1)  # retry: never again
    assert killed == []
    await acts._maybe_crash("generate_sql", attempt=1, tool_calls_so_far=1)
    assert killed == [(os.getpid(), signal.SIGKILL)]


# ---------------------------------------------------------------------------------------------
# resume from checkpoint
# ---------------------------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def stack(
    tmp_path_factory: pytest.TempPathFactory, lakehouse_path: Path
) -> Iterator[tuple[Settings, AppState]]:
    index_path = tmp_path_factory.mktemp("index")
    embedder = HashEmbedder()
    build_index(GOVERNANCE_DIR, index_path, embedder)
    port = _free_port()
    settings = Settings(
        _env_file=None,
        lakehouse_path=lakehouse_path,
        index_path=index_path,
        governance_path=GOVERNANCE_DIR,
        run_sql_capability_token="t",
        mcp_server_url=f"http://127.0.0.1:{port}/mcp",
        llm_cache=False,
        anthropic_api_key="sk-ant-fake",  # pragma: allowlist secret
    )
    state = AppState.from_settings(settings, embedder=embedder)
    app = create_server(state).streamable_http_app(streamable_http_path="/mcp")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield settings, state
    server.should_exit = True
    thread.join(timeout=5)


def _msg(content: list[dict[str, Any]], stop: str) -> BetaMessage:
    return BetaMessage.model_validate(
        {
            "id": "msg_x",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-5-5",
            "content": content,
            "stop_reason": stop,
            "stop_sequence": None,
            "usage": {"input_tokens": 100, "output_tokens": 20},
        }
    )


class ScriptedClaude:
    """Fake AsyncAnthropic: records every request; answers based on conversation shape."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.beta = self
        self.messages = self

    async def create(self, **request: Any) -> BetaMessage:
        self.requests.append(request)
        last = request["messages"][-1]
        has_tool_result = isinstance(last["content"], list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in last["content"]
        )
        if not has_tool_result:
            return _msg(
                [{"type": "tool_use", "id": "tu_1", "name": "get_schema", "input": {}}], "tool_use"
            )
        final = (
            '{"sql": "SELECT count(*) AS n FROM orders", "explanation": "count",'
            ' "self_reported_risk": "safe"}'
        )
        return _msg([{"type": "text", "text": final}], "end_turn")


def _inputs() -> tuple[AnalystRequest, RetrievedContext, QueryPlan]:
    req = AnalystRequest(question="How many orders are there?", requester="t")
    ctx = RetrievedContext(
        passages=[
            Passage(
                source="policies.md",
                section="s",
                citation="policies.md#row-limits",
                score=1.0,
                text="x",
            )
        ],
        schema_ddl="CREATE TABLE orders (order_id BIGINT);",
        pii_columns=[],
    )
    plan = QueryPlan(tables=["orders"], expected_output_shape="scalar", risk_assessment="safe")
    return req, ctx, plan


async def test_generate_sql_fresh_run_makes_two_claude_calls(
    stack: tuple[Settings, AppState],
) -> None:
    settings, state = stack
    claude = ScriptedClaude()
    acts = AnalystActivities(
        settings=settings,
        llm=LLM(settings, client=claude),
        lakehouse=state.lakehouse,
        policy=state.policy,
        index=state.index,  # type: ignore[arg-type]
    )
    env = ActivityEnvironment()
    heartbeats: list[Any] = []
    env.on_heartbeat = lambda *d: heartbeats.append(d[0])
    out = await env.run(acts.generate_sql, *_inputs())
    assert out.sql.startswith("SELECT count(*)")
    assert out.tool_calls == ["get_schema"] and out.resumed_from_checkpoint is False
    assert len(claude.requests) == 2
    assert len(heartbeats) == 1 and heartbeats[0]["tool_calls"] == ["get_schema"]
    assert len(heartbeats[0]["messages"]) == 3  # user, assistant(tool_use), user(tool_result)


async def test_generate_sql_resumes_from_checkpoint_without_replaying(
    stack: tuple[Settings, AppState],
) -> None:
    settings, state = stack
    # 1) produce a real checkpoint by running once
    first = ScriptedClaude()
    acts = AnalystActivities(
        settings=settings,
        llm=LLM(settings, client=first),
        lakehouse=state.lakehouse,
        policy=state.policy,
        index=state.index,  # type: ignore[arg-type]
    )
    env = ActivityEnvironment()
    heartbeats: list[Any] = []
    env.on_heartbeat = lambda *d: heartbeats.append(d[0])
    await env.run(acts.generate_sql, *_inputs())
    checkpoint = heartbeats[0]

    # 2) retry with that checkpoint in heartbeat details, as the server would provide it
    second = ScriptedClaude()
    acts.llm = LLM(settings, client=second)  # type: ignore[arg-type]
    env2 = ActivityEnvironment()
    env2.info = dataclasses.replace(
        ActivityEnvironment.default_info(), attempt=2, heartbeat_details=[checkpoint]
    )
    out = await env2.run(acts.generate_sql, *_inputs())

    assert out.resumed_from_checkpoint is True
    assert out.tool_calls == ["get_schema"]  # from the checkpoint, not re-executed
    assert len(second.requests) == 1  # only the final turn; the first turn was not replayed
    assert any(
        b.get("type") == "tool_result" for b in second.requests[0]["messages"][-1]["content"]
    )
    assert out.usage.calls == 2  # checkpointed usage (1) + this attempt's call (1)
