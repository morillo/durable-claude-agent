# ADR-010: Evals run the real workflow on a private Temporal dev server

**Decision.** `make eval` starts `WorkflowEnvironment.start_local`, an in-process MCP server,
and a real worker, then runs one workflow per question and scores from the results and the
event history.

**Alternatives.** (a) Call the activity functions directly: faster and simpler, but blind to
orchestration bugs (a gate that executes before approval, a serialization failure in a result
model, a retry that double-runs). (b) Run against the developer's long-lived dev server:
convenient, but runs would mix with manual experiments and the harness would depend on a
process it does not control. (c) Replay recorded histories: excellent for regression tests of
workflow code, not a measure of answer quality.

**Why this.** The approval metric is only meaningful if it is read from what Temporal actually
scheduled; the first eval run found a prompt bug exactly there. The private dev server makes
runs isolated and reproducible, concurrency 4 keeps a 32-question run under three minutes, and
the response cache keeps iteration cheap. The judge is cached and sees the evidence the agent
saw, after the first iteration showed that an uninformed judge measures its own blind spots.
