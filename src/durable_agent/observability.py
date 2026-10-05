"""OpenTelemetry tracing: workflow -> activities -> Claude calls -> MCP tool calls, into Phoenix.

Ported from morillo/langgraph-travel-assistant: the app depends only on the OpenTelemetry SDK
and OpenInference semantic conventions, never on a vendor client, so the trace backend is any
OTLP collector. Phoenix is the local default because it renders LLM spans (prompts, tool calls,
token counts) natively.

Who emits what
--------------
* **Temporal** ``TracingInterceptor`` on the client: spans for StartWorkflow, RunWorkflow (as
  zero-duration completed spans, since open spans cannot survive replay), RunActivity, signals
  and queries. Everything below nests under the activity span.
* **AnthropicInstrumentor**: one LLM span per Messages API call with model, prompt, completion,
  and token usage. ``llm.py`` wraps each call in a parent span carrying our cost estimate and
  whether the response came from the local cache.
* **MCPInstrumentor**: propagates trace context from the MCP client (in the worker) to the MCP
  server process, so the server's own ``tool.*`` spans attach under the activity that called them.

Disable everything with ``OTEL_ENABLED=false``; with no provider configured the tracers are
no-ops and cost nothing.
"""

from __future__ import annotations

import logging

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from durable_agent.config import Settings

log = logging.getLogger(__name__)
_provider: TracerProvider | None = None


def setup_tracing(settings: Settings, service_name: str) -> TracerProvider | None:
    """Configure the global tracer provider once per process. Returns None when disabled."""
    global _provider
    if not settings.otel_enabled:
        return None
    if _provider is not None:
        return _provider
    resource = Resource.create(
        {
            "service.name": service_name,
            # Phoenix groups traces into projects by this attribute.
            "openinference.project.name": settings.phoenix_project_name,
        }
    )
    provider = TracerProvider(resource=resource)
    endpoint = settings.phoenix_collector_endpoint.rstrip("/") + "/v1/traces"
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)

    from openinference.instrumentation.anthropic import AnthropicInstrumentor

    AnthropicInstrumentor().instrument(tracer_provider=provider)
    try:
        from openinference.instrumentation.mcp import MCPInstrumentor

        MCPInstrumentor().instrument(tracer_provider=provider)
    except Exception as e:  # context propagation is a nice-to-have; never block startup on it
        log.warning("MCP instrumentation unavailable: %s", e)
    _provider = provider
    log.info(
        "tracing on: service=%s project=%s -> %s",
        service_name,
        settings.phoenix_project_name,
        endpoint,
    )
    return provider


def shutdown_tracing() -> None:
    """Flush pending spans (short-lived processes like the CLI need this before exit)."""
    global _provider
    if _provider is not None:
        _provider.force_flush(timeout_millis=5000)
        _provider.shutdown()
        _provider = None


def tracer(name: str) -> trace.Tracer:
    """A tracer that is a no-op until ``setup_tracing`` has run."""
    return trace.get_tracer(name)
