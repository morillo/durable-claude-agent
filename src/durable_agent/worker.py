"""Temporal worker: hosts the workflow and the activities. ``make worker`` runs this."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from datetime import timedelta

from temporalio.client import Client, Interceptor
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

from durable_agent.activities import AnalystActivities
from durable_agent.config import Settings, get_settings
from durable_agent.observability import setup_tracing, shutdown_tracing
from durable_agent.workflows import AnalystWorkflow, workflow_runner

log = logging.getLogger("durable_agent.worker")


async def connect(settings: Settings) -> Client:
    """Temporal client with the pydantic converter and, when tracing is on, OTel spans."""
    interceptors: list[Interceptor] = [TracingInterceptor()] if settings.otel_enabled else []
    return await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
        data_converter=pydantic_data_converter,
        interceptors=interceptors,
    )


async def run_worker(settings: Settings) -> None:
    setup_tracing(settings, service_name="durable-agent-worker")
    client = await connect(settings)
    acts = AnalystActivities.from_settings(settings)
    log.info(
        "worker ready: queue=%s temporal=%s mcp=%s models=%s/%s crash_hook=%s",
        settings.temporal_task_queue,
        settings.temporal_address,
        settings.mcp_server_url,
        settings.model_fast,
        settings.model_strong,
        settings.demo_crash_at or "off",
    )
    async with Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=[AnalystWorkflow],
        activities=acts.all,
        workflow_runner=workflow_runner(),
        # Flush heartbeats promptly so a checkpoint is on the server before a crash.
        default_heartbeat_throttle_interval=timedelta(seconds=1),
        max_heartbeat_throttle_interval=timedelta(seconds=2),
    ):
        await asyncio.Event().wait()  # run until killed


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run_worker(get_settings()))
    shutdown_tracing()
    return 0


if __name__ == "__main__":
    sys.exit(main())
