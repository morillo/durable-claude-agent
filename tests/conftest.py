"""Shared fixtures: a small seeded lakehouse built once per test session."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from durable_agent.lakehouse import Lakehouse
from durable_agent.risk import Policy, load_policy
from durable_agent.seed import seed_lakehouse

GOVERNANCE_DIR = Path(__file__).resolve().parents[1] / "governance"


@pytest.fixture(scope="session")
def lakehouse_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("lakehouse")
    seed_lakehouse(out, scale=0.1)
    return out


@pytest.fixture(scope="session")
def lakehouse(lakehouse_path: Path) -> Iterator[Lakehouse]:
    lh = Lakehouse(lakehouse_path)
    yield lh
    lh.close()


@pytest.fixture(scope="session")
def schema(lakehouse: Lakehouse) -> dict[str, dict[str, str]]:
    return lakehouse.schema()


@pytest.fixture(scope="session")
def policy() -> Policy:
    return load_policy(GOVERNANCE_DIR / "policies.md")
