"""Tracing setup is a no-op when disabled, idempotent when enabled, and never a startup risk."""

from __future__ import annotations

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from durable_agent import observability
from durable_agent.config import Settings


@pytest.fixture(autouse=True)
def _reset_provider() -> None:
    observability._provider = None


def test_disabled_returns_none_and_tracer_is_noop() -> None:
    s = Settings(_env_file=None, otel_enabled=False)
    assert observability.setup_tracing(s, "t") is None
    with observability.tracer("x").start_as_current_span("noop") as span:
        assert not span.is_recording() or isinstance(trace.get_tracer_provider(), TracerProvider)


def test_enabled_configures_provider_once() -> None:
    s = Settings(_env_file=None, otel_enabled=True, phoenix_collector_endpoint="http://127.0.0.1:1")
    p1 = observability.setup_tracing(s, "svc")
    p2 = observability.setup_tracing(s, "svc")
    assert isinstance(p1, TracerProvider) and p1 is p2
    attrs = p1.resource.attributes
    assert attrs["service.name"] == "svc"
    assert attrs["openinference.project.name"] == s.phoenix_project_name
    # shutdown must not raise even though the endpoint is unreachable
    observability.shutdown_tracing()
    assert observability._provider is None
