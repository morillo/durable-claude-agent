"""Anthropic client wrapper: model routing, structured outputs, cost accounting, response cache.

Design notes
------------
* **Two models, one place.** ``settings.model_fast`` (Haiku) plans and summarizes;
  ``settings.model_strong`` (Sonnet) writes SQL with tools. Nothing else picks a model.
* **Structured outputs, never prefill.** Plans and summaries use ``output_config.format`` with a
  JSON schema derived from the pydantic model, so parsing cannot fail on markdown fences.
  Assistant prefill is rejected by current models anyway.
* **Thinking stays on; effort is the dial.** Sonnet 5.5 cannot disable thinking; we set
  ``output_config.effort`` instead. Haiku 4.5 does not accept ``effort``, so it is only sent to
  models that support it.
* **Refusal fallbacks on by default.** ``fallbacks="default"`` with the matching beta header lets
  the API re-run a safety-declined request on a fallback model inside the same call.
* **Every call is accounted for.** ``LLMUsage`` carries tokens and an estimated cost computed
  from a small price table; the workflow sums these and the summary reports total cost.
* **Response cache (idempotency).** Requests are hashed (model + system + messages + tools +
  output config); identical requests return the cached response without an API call. This makes
  activity retries and eval re-runs free, and is what the crash demo relies on alongside
  heartbeat checkpoints. Disable with ``LLM_CACHE=false``.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TypeVar

import anthropic
from anthropic.types.beta import BetaMessage
from pydantic import BaseModel

from durable_agent.config import Settings
from durable_agent.models import LLMUsage

T = TypeVar("T", bound=BaseModel)

# USD per million tokens. Cache writes cost 1.25x input; cache reads 0.1x input.
PRICES: dict[str, dict[str, float]] = {
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00, "cache_read": 0.10, "cache_write": 1.25},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00, "cache_read": 0.20, "cache_write": 2.50},
    "claude-sonnet-5": {"input": 2.00, "output": 10.00, "cache_read": 0.20, "cache_write": 2.50},
    "claude-opus-5-5": {"input": 4.00, "output": 20.00, "cache_read": 0.20, "cache_write": 5.00},
}
FALLBACK_BETA = "server-side-fallback-2026-07-01"
NO_EFFORT_MODELS = ("claude-haiku-4-5",)


def estimate_cost(
    model: str, input_tokens: int, output_tokens: int, cache_read: int, cache_write: int
) -> float:
    p = PRICES.get(model)
    if p is None:
        return 0.0
    usd = (
        input_tokens * p["input"]
        + output_tokens * p["output"]
        + cache_read * p["cache_read"]
        + cache_write * p["cache_write"]
    ) / 1_000_000
    return round(usd, 6)


def usage_from_message(message: BetaMessage, *, cached: bool = False) -> LLMUsage:
    u = message.usage
    cache_read = u.cache_read_input_tokens or 0
    cache_write = u.cache_creation_input_tokens or 0
    return LLMUsage(
        model=message.model,
        calls=0 if cached else 1,
        input_tokens=0 if cached else u.input_tokens,
        output_tokens=0 if cached else u.output_tokens,
        cache_read_tokens=0 if cached else cache_read,
        cache_write_tokens=0 if cached else cache_write,
        cost_usd=0.0
        if cached
        else estimate_cost(message.model, u.input_tokens, u.output_tokens, cache_read, cache_write),
        cached_responses=1 if cached else 0,
    )


class ResponseCache:
    """SQLite key/value store of serialized ``BetaMessage`` objects keyed by request hash."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._con = sqlite3.connect(str(path), check_same_thread=False)
        self._con.execute(
            "CREATE TABLE IF NOT EXISTS responses (key TEXT PRIMARY KEY, body TEXT NOT NULL)"
        )
        self._con.commit()

    @staticmethod
    def key(request: dict[str, Any]) -> str:
        canonical = json.dumps(request, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode()).hexdigest()

    def get(self, key: str) -> BetaMessage | None:
        with self._lock:
            row = self._con.execute("SELECT body FROM responses WHERE key = ?", (key,)).fetchone()
        return BetaMessage.model_validate_json(row[0]) if row else None

    def put(self, key: str, message: BetaMessage) -> None:
        with self._lock:
            self._con.execute(
                "INSERT OR REPLACE INTO responses (key, body) VALUES (?, ?)",
                (key, message.model_dump_json()),
            )
            self._con.commit()


class LLM:
    """The only object in the codebase that talks to the Anthropic API."""

    def __init__(self, settings: Settings, client: anthropic.AsyncAnthropic | None = None) -> None:
        self.settings = settings
        # Settings own the key (loaded from .env); the SDK would otherwise only read os.environ.
        api_key = (
            settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None
        )
        self.client = client or anthropic.AsyncAnthropic(
            api_key=api_key, max_retries=2, timeout=120.0
        )
        self.cache = ResponseCache(settings.llm_cache_path) if settings.llm_cache else None

    # -- core ---------------------------------------------------------------------------------

    async def create(
        self,
        *,
        model: str,
        system: str,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
        output_schema: dict[str, Any] | None = None,
        max_tokens: int = 4096,
        effort: str | None = None,
    ) -> tuple[BetaMessage, LLMUsage]:
        """One Messages API call with caching, fallbacks, and usage accounting."""
        request: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            # The system prompt is the stable prefix: cache it. Tools render before system
            # in the cache prefix, so a stable tool list is also required for hits.
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": list(messages),
        }
        if tools:
            request["tools"] = list(tools)
        output_config: dict[str, Any] = {}
        if output_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": output_schema}
        if effort and not model.startswith(NO_EFFORT_MODELS):
            output_config["effort"] = effort
        if output_config:
            request["output_config"] = output_config
        if self.settings.anthropic_fallbacks and model == self.settings.model_strong:
            request["fallbacks"] = "default"
            request["betas"] = [FALLBACK_BETA]

        key = ResponseCache.key(request)
        if self.cache is not None:
            hit = self.cache.get(key)
            if hit is not None:
                return hit, usage_from_message(hit, cached=True)

        message = await self.client.beta.messages.create(**request)
        if message.stop_reason == "refusal":
            raise LLMRefusalError(message)
        if self.cache is not None and message.stop_reason in ("end_turn", "tool_use"):
            self.cache.put(key, message)
        return message, usage_from_message(message)

    async def structured(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: type[T],
        max_tokens: int = 2048,
        effort: str | None = None,
    ) -> tuple[T, LLMUsage]:
        """Single-turn call whose text output is guaranteed to validate against ``schema``."""
        message, usage = await self.create(
            model=model,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_schema=_strict_schema(schema),
            max_tokens=max_tokens,
            effort=effort,
        )
        return schema.model_validate_json(text_of(message)), usage


class LLMRefusalError(RuntimeError):
    """The model (and any fallback) declined the request on safety grounds."""

    def __init__(self, message: BetaMessage) -> None:
        details = getattr(message, "stop_details", None)
        category = getattr(details, "category", None) if details else None
        super().__init__(f"model refused the request (category={category})")
        self.message_obj = message


def text_of(message: BetaMessage) -> str:
    return "".join(getattr(b, "text", "") for b in message.content if b.type == "text").strip()


def _strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Pydantic JSON schema tightened for the API: no additional properties, all required."""
    schema = model.model_json_schema()

    def tighten(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"])
            for v in node.values():
                tighten(v)
        elif isinstance(node, list):
            for v in node:
                tighten(v)

    tighten(schema)
    return schema
