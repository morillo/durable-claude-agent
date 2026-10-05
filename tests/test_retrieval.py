"""Chunking, embedding, and LanceDB index behaviour, with the deterministic hash embedder."""

from __future__ import annotations

from pathlib import Path

import pytest

from durable_agent.retrieval import (
    GovernanceIndex,
    HashEmbedder,
    build_index,
    chunk_markdown,
    load_corpus,
)
from durable_agent.retrieval.chunking import slugify
from durable_agent.risk import SECTIONS
from tests.conftest import GOVERNANCE_DIR

SAMPLE = """# Title

Preamble that belongs to no section.

## Alpha

Alpha body line 1.

### Alpha > child

Child body.

```sql
-- # not a heading inside a code block
SELECT 1;
```

## Beta

Beta body.

## Empty
"""


def test_chunk_markdown_structure() -> None:
    chunks = chunk_markdown(SAMPLE, "sample.md")
    sections = [c.section for c in chunks]
    assert sections == ["Alpha", "Alpha > Alpha > child", "Beta"]
    child = chunks[1]
    assert child.heading == "Alpha > child"
    assert "SELECT 1;" in child.text and "not a heading" in child.text
    assert child.source == "sample.md"
    assert child.embed_text.startswith("Alpha > Alpha > child\n")
    # ids are stable across runs
    assert chunks[0].id == chunk_markdown(SAMPLE, "sample.md")[0].id


def test_slugify_matches_github_style() -> None:
    assert slugify("Query shape rules") == "query-shape-rules"
    assert slugify("Table: `customers`") == "table-customers"
    assert slugify("Average order value (AOV)") == "average-order-value-aov"
    assert slugify("Table: payment_cards") == "table-payment_cards"


def test_governance_corpus_covers_every_risk_section() -> None:
    """Every section the classifier cites must exist as a retrievable chunk."""
    sections = {c.section for c in load_corpus(GOVERNANCE_DIR) if c.source == "policies.md"}
    missing = set(SECTIONS.values()) - sections
    assert not missing, missing


def test_hash_embedder_is_deterministic_and_normalized() -> None:
    e = HashEmbedder(dim=64)
    a, b = e.embed(["net revenue excludes refunds", "net revenue excludes refunds"])
    assert a == b and len(a) == 64
    assert abs(sum(x * x for x in a) - 1.0) < 1e-6
    (c,) = e.embed(["payment cards are restricted"])
    sim_same = sum(x * y for x, y in zip(a, e.embed(["revenue refunds"])[0], strict=True))
    sim_other = sum(x * y for x, y in zip(a, c, strict=True))
    assert sim_same > sim_other


@pytest.fixture(scope="module")
def index(tmp_path_factory: pytest.TempPathFactory) -> GovernanceIndex:
    out = tmp_path_factory.mktemp("index")
    embedder = HashEmbedder()
    n = build_index(GOVERNANCE_DIR, out, embedder)
    assert n > 20
    return GovernanceIndex(out, embedder)


def test_index_search_returns_cited_passages(index: GovernanceIndex) -> None:
    hits = index.search("which customer columns are PII and need approval", k=3)
    assert len(hits) == 3
    assert hits[0].section == "Classification of data > PII columns"
    assert hits[0].citation == "policies.md#pii-columns"
    assert hits[0].score >= hits[1].score >= hits[2].score
    assert "customers.email" in hits[0].text


def test_index_search_finds_metric_definitions(index: GovernanceIndex) -> None:
    hits = index.search("how is net revenue computed, does it exclude refunds", k=2)
    assert any(h.section == "Business definitions > Net revenue" for h in hits)
    assert all(h.source == "data_dictionary.md" for h in hits)


def test_index_rejects_mismatched_embedder(index: GovernanceIndex) -> None:
    with pytest.raises(ValueError, match="Rebuild"):
        GovernanceIndex(index.path, HashEmbedder(dim=32))


def test_index_missing_is_clear(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="make index"):
        GovernanceIndex(tmp_path, HashEmbedder())
