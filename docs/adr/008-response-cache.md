# ADR-008: Request-hash response cache for idempotent retries and cheap evals

**Decision.** `llm.py` hashes every Messages API request (model, system, messages, tools, output
config) and stores the response in SQLite. Identical requests return the cached response with
zero cost; `LLM_CACHE=false` disables it, and the crash demo runs with it off.

**Alternatives.** (a) No cache: every retry and every eval re-run pays full price, and a retried
activity can produce a different answer than the attempt that failed. (b) Prompt caching alone:
reduces input cost on the stable prefix but still bills every call. (c) Temporal-side
idempotency keys: Temporal guarantees completed *activities* are not re-run, but it cannot
make the API calls inside an interrupted activity idempotent.

**Why this.** It is the idempotency layer that Temporal does not provide, it makes eval
re-runs after harness changes nearly free (the third iteration cost five cents), and the cache
hit is recorded on the span and in the usage ledger so nobody mistakes a warm run for a cold
one. Trade-off: with the cache on, asking the same question twice returns the same answer
without a new call, which is right for a demo and for evals and would be a per-tenant policy
decision in production.
