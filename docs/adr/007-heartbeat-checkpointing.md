# ADR-007: Checkpoint the tool loop in activity heartbeat details

**Decision.** `generate_sql` writes its conversation, tool-call list, usage, and validation flag
into `activity.heartbeat(details)` after every tool round and on a 3 s pulse during long calls.
On retry it reads `heartbeat_details` and resumes mid-conversation.

**Alternatives.** (a) One activity per LLM turn, orchestrated by the workflow: each turn becomes
a durable step, but the conversation then crosses the workflow boundary on every turn (payload
size, history growth) and the loop logic moves into deterministic workflow code. (b) An external
store (Redis, a table) for the partial conversation: works, but adds infrastructure and a
second source of truth beside Temporal. (c) Accept re-running the whole activity on retry and
rely on the response cache to make it cheap: simple, but the retry still repeats API requests.

**Why this.** Heartbeat details are the Temporal-native place for "where was I": stored on the
server, delivered to the retry, no extra infrastructure. The demo shows the server holding the
checkpoint with no worker alive. Limits worth knowing: details must be small (a short tool
conversation is), the SDK sends heartbeats on the event loop so a blocking sleep before a crash
loses the checkpoint (found the hard way, now a test), and a turn that finishes after the last
heartbeat is repeated on retry, which the response cache then serves for free.
