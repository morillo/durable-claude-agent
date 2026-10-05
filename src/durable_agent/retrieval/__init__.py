"""Local, zero-token retrieval over ``governance/*.md``.

Pipeline: :mod:`chunking` splits Markdown by heading into citable sections,
:mod:`embeddings` turns text into vectors with a local sentence-transformers model,
and :mod:`index` persists them in LanceDB and answers top-k queries.
"""

from durable_agent.retrieval.chunking import Chunk, chunk_markdown, load_corpus
from durable_agent.retrieval.embeddings import Embedder, HashEmbedder, SentenceTransformerEmbedder
from durable_agent.retrieval.index import GovernanceIndex, Passage, build_index

__all__ = [
    "Chunk",
    "Embedder",
    "GovernanceIndex",
    "HashEmbedder",
    "Passage",
    "SentenceTransformerEmbedder",
    "build_index",
    "chunk_markdown",
    "load_corpus",
]
