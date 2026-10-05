# ADR-001: Temporal workflows for the agent loop's durability

**Decision.** Each analyst question runs as one Temporal workflow with six activities; the
approval gate is a signal with a timeout.

**Alternatives.** (a) A plain Python loop with retries around each LLM call: cheapest to write,
but a worker crash loses the run, an approval that takes hours needs a database and a scheduler
of its own, and there is no history to audit. (b) An agent framework with a checkpointer
(LangGraph and similar): gives resumable state, but the checkpointer is a library inside the
same process, retries and timeouts are still hand-rolled, and the human-approval wait is an
interrupt that the host must persist and resume. (c) A queue plus a state machine in a database:
durable, but the retry, timeout, heartbeat, and history machinery would be rebuilt by hand.

**Why Temporal.** It provides exactly the primitives an agent step needs, retries with
backoff, per-step timeouts, heartbeats, a durable wait on a human signal, replayable history,
queryable state and search attributes, and it does so outside the process that can crash. The
crash demo (`make demo-crash`) is the argument in one screen. The cost is a server to run and
the determinism discipline in workflow code, which this repo keeps small by putting every
side effect in an activity. Scope was limited to one workflow, one task queue, and the features
the story needs: no child workflows, no schedules, no Nexus.
