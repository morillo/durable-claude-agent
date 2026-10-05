# ADR-003: LanceDB with local MiniLM embeddings for governance retrieval

**Decision.** `governance/*.md` is chunked by heading, embedded with `all-MiniLM-L6-v2` via
sentence-transformers on CPU, and stored in LanceDB under `data/index/`.

**Alternatives.** (a) ChromaDB: the most common local choice, but 1.x ships a Rust server
binary and heavier dependencies, and a 22-chunk corpus gains nothing from them. (b) A hosted
vector database: a cloud dependency the repo must not have. (c) No vector store at all, just
put both documents in the prompt: viable at this corpus size, but it would not demonstrate
retrieval, would not scale to a real governance library, and would make the retrieval recall
metric meaningless. (d) API embeddings: cost tokens on every run and every eval.

**Why LanceDB.** It is embedded and Arrow-native, lives in a directory next to the Delta tables,
needs no server process, and is queried by the same Arrow tooling as the rest of the lakehouse.
Local embeddings cost zero tokens. Chunks keep their heading path, the same identifier the risk
classifier cites, so answers cite sections consistently. Known limit, visible in the baseline
recall of 89%: a small embedding model misses lexical matches; hybrid BM25 + vector is the
next step.
