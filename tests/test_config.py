"""Settings load from env with validation; secrets never leak through repr."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from durable_agent.config import Settings


def test_defaults_do_not_require_api_key() -> None:
    s = Settings(_env_file=None)
    assert s.anthropic_api_key is None
    assert s.model_fast == "claude-haiku-4-5"
    assert s.model_strong == "claude-sonnet-5-5"
    assert s.max_rows == 1000


def test_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_STRONG", "claude-opus-5-5")
    monkeypatch.setenv("MAX_ROWS", "50")
    s = Settings(_env_file=None)
    assert s.model_strong == "claude-opus-5-5"
    assert s.max_rows == 50


def test_invalid_max_rows_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_ROWS", "0")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_secrets_are_masked_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")
    monkeypatch.setenv("RUN_SQL_CAPABILITY_TOKEN", "cap-token-value")
    s = Settings(_env_file=None)
    text = repr(s)
    assert "sk-ant-not-a-real-key" not in text
    assert "cap-token-value" not in text
    assert s.run_sql_capability_token is not None
    assert s.run_sql_capability_token.get_secret_value() == "cap-token-value"
