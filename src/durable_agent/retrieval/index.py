"""Build and query the LanceDB governance index.

Why LanceDB (ADR-003): it is an embedded, Arrow-native store that lives in a directory next to
the Delta tables, needs no server process, and is queried by the same Arrow tooling as the
rest of the lakehouse. A corpus of a few dozen chunks does not need an ANN index at all, so
searches are exact cosine scans. ``meta.json`` records which embedder built the index so a
query with a different model fails loudly instead of returning garbage.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

import lancedb

from durable_agent.retrieval.chunking import load_corpus
from durable_agent.retrieval.embeddings import DEFAULT_MODEL, Embedder, SentenceTransformerEmbedder

TABLE = "governance"
META = "meta.json"


@dataclass(frozen=True)
class Passage:
    source: str
    section: str
    heading: str
    anchor: str
    text: str
    score: float  # cosine similarity in [-1, 1]; higher is better

    @property
    def citation(self) -> str:
        return f"{self.source}#{self.anchor}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["citation"] = self.citation
        return d


def build_index(governance_dir: Path, index_path: Path, embedder: Embedder) -> int:
    """(Re)build the index from scratch. Returns the number of chunks written."""
    chunks = load_corpus(governance_dir)
    if not chunks:
        raise ValueError(f"No markdown chunks found under {governance_dir}")
    vectors = embedder.embed([c.embed_text for c in chunks])
    rows = [
        {
            "id": c.id,
            "source": c.source,
            "section": c.section,
            "heading": c.heading,
            "anchor": c.anchor,
            "text": c.text,
            "vector": v,
        }
        for c, v in zip(chunks, vectors, strict=True)
    ]
    index_path.mkdir(parents=True, exist_ok=True)
    db = lancedb.connect(str(index_path))
    db.create_table(TABLE, data=rows, mode="overwrite")
    (index_path / META).write_text(
        json.dumps({"embedder": embedder.name, "dim": embedder.dim, "chunks": len(rows)}, indent=2)
        + "\n"
    )
    return len(rows)


class GovernanceIndex:
    """Read side. Opens an existing index and answers top-k similarity queries."""

    def __init__(self, index_path: Path, embedder: Embedder) -> None:
        self.path = Path(index_path)
        meta_file = self.path / META
        if not meta_file.exists():
            raise FileNotFoundError(f"No index at {self.path}. Run `make index`.")
        self.meta = json.loads(meta_file.read_text())
        if self.meta["embedder"] != embedder.name:
            raise ValueError(
                f"Index was built with {self.meta['embedder']!r} but query embedder is "
                f"{embedder.name!r}. Rebuild with `make index`."
            )
        self.embedder = embedder
        self._table = lancedb.connect(str(self.path)).open_table(TABLE)

    def search(self, query: str, k: int = 4) -> list[Passage]:
        (vector,) = self.embedder.embed([query])
        # The stub types search() as the generic builder; the vector builder adds distance_type.
        builder = cast(Any, self._table.search(vector, vector_column_name="vector"))
        hits: list[dict[str, Any]] = builder.distance_type("cosine").limit(max(1, k)).to_list()
        return [
            Passage(
                source=h["source"],
                section=h["section"],
                heading=h["heading"],
                anchor=h["anchor"],
                text=h["text"],
                score=round(1.0 - float(h["_distance"]), 4),
            )
            for h in hits
        ]

    def count(self) -> int:
        return int(self._table.count_rows())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the LanceDB index over governance/*.md.")
    parser.add_argument("--governance", type=Path, default=Path("governance"))
    parser.add_argument("--out", type=Path, default=Path("data/index"))
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args(argv)
    embedder = SentenceTransformerEmbedder(args.model)
    n = build_index(args.governance, args.out, embedder)
    print(f"  indexed {n} chunks from {args.governance}")
    print(f"  embedder {embedder.name} ({embedder.dim}d) -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
