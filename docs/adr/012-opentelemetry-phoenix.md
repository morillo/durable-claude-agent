# ADR-012: OpenTelemetry with OpenInference instrumentors, exported to Phoenix

**Decision.** Every process (CLI, worker, MCP server) configures the OpenTelemetry SDK with an
OTLP/HTTP exporter. Temporal's tracing interceptor links the workflow and its activities,
the OpenInference instrumentors for Anthropic and MCP add LLM and tool spans, and `llm.py` adds
an `llm.request` span with cost and cache-hit attributes. Spans go to Arize Phoenix, which runs
locally in Docker (`make up`, or `make phoenix`). `OTEL_ENABLED=false` turns it off.

**Alternatives.** (a) Logs only: each process logs its own story, but one run crosses three
processes and a server, and logs cannot show which Claude call, which tool call, and which SQL
belong to the same question. (b) A hosted LLM observability service (LangSmith, Langfuse
Cloud, and similar): good LLM views, but an account and a cloud dependency the repo must not
have. (c) A generic tracing backend such as Jaeger: standard OTLP, but no notion of model,
tokens, prompts, or tool calls, so the LLM spans would be opaque key-value lists.

**Why this.** OpenTelemetry is the vendor-neutral standard and the Temporal SDK, the Anthropic
SDK and MCP all have instrumentation for it, so one trace follows a run from `StartWorkflow`
through each activity, each Claude call, and across the MCP client/server boundary to the SQL
the tool ran. OpenInference is the semantic convention Phoenix reads natively, which gives
token counts, model names, and tool calls their own views. Phoenix needs no account and runs
next to Temporal. Changing backend is changing the OTLP endpoint, not the instrumentation.
