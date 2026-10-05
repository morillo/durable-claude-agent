"""Cost math, request hashing, strict-schema tightening, and the response cache."""

from __future__ import annotations

from pathlib import Path

from anthropic.types.beta import BetaMessage
from pydantic import BaseModel, Field

from durable_agent.llm import ResponseCache, _strict_schema, estimate_cost, usage_from_message
from durable_agent.models import LLMUsage


def _message(model: str = "claude-sonnet-5-5") -> BetaMessage:
    return BetaMessage.model_validate(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": '{"a": 1}'}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 200,
                "cache_read_input_tokens": 3000,
                "cache_creation_input_tokens": 500,
            },
        }
    )


def test_estimate_cost_uses_price_table() -> None:
    # 1000 in @ $2 + 200 out @ $10 + 3000 cache read @ $0.20 + 500 cache write @ $2.50 per MTok
    expected = (1000 * 2 + 200 * 10 + 3000 * 0.20 + 500 * 2.50) / 1_000_000
    assert estimate_cost("claude-sonnet-5-5", 1000, 200, 3000, 500) == round(expected, 6)
    assert estimate_cost("unknown-model", 1000, 200, 0, 0) == 0.0


def test_usage_from_message_and_cached_variant() -> None:
    live = usage_from_message(_message())
    assert live.calls == 1 and live.input_tokens == 1000 and live.cache_read_tokens == 3000
    assert live.cost_usd > 0
    cached = usage_from_message(_message(), cached=True)
    assert cached.calls == 0 and cached.cost_usd == 0 and cached.cached_responses == 1


def test_usage_add_tracks_mixed_models() -> None:
    a = LLMUsage(model="claude-haiku-4-5", calls=1, input_tokens=10, cost_usd=0.001)
    b = LLMUsage(model="claude-sonnet-5-5", calls=2, input_tokens=20, cost_usd=0.002)
    total = a.add(b)
    assert total.model == "mixed" and total.calls == 3 and total.input_tokens == 30
    assert total.cost_usd == 0.003
    assert a.add(LLMUsage(model="claude-haiku-4-5")).model == "claude-haiku-4-5"


def test_strict_schema_tightens_objects() -> None:
    class Inner(BaseModel):
        x: int

    class Outer(BaseModel):
        name: str = Field(max_length=10)
        items: list[Inner]
        maybe: str | None = None
        score: int = Field(ge=0, le=1)

    schema = _strict_schema(Outer)
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"name", "items", "maybe", "score"}
    assert "maxLength" not in schema["properties"]["name"]
    assert (
        "minimum" not in schema["properties"]["score"]
        and "maximum" not in schema["properties"]["score"]
    )
    inner = schema["$defs"]["Inner"]
    assert inner["additionalProperties"] is False and inner["required"] == ["x"]


def test_response_cache_roundtrip_and_key_stability(tmp_path: Path) -> None:
    cache = ResponseCache(tmp_path / "c.sqlite")
    req_a = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "system": "s"}
    req_b = {
        "system": "s",
        "messages": [{"role": "user", "content": "hi"}],
        "model": "m",
    }  # same, reordered
    assert ResponseCache.key(req_a) == ResponseCache.key(req_b)
    assert cache.get(ResponseCache.key(req_a)) is None
    cache.put(ResponseCache.key(req_a), _message())
    hit = cache.get(ResponseCache.key(req_b))
    assert hit is not None and hit.id == "msg_1"
    assert ResponseCache.key({**req_a, "model": "other"}) != ResponseCache.key(req_a)
