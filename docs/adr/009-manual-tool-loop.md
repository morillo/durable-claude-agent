# ADR-009: Manual Messages API tool loop instead of the SDK tool runner

**Decision.** `generate_sql` drives the request / tool-call / tool-result cycle itself, with MCP
tool definitions converted to Anthropic tool schemas and results appended as `tool_result`
blocks.

**Alternatives.** (a) `client.beta.messages.tool_runner` with the SDK's MCP helpers: less code
and the recommended default for a custom-tool agent, but the loop has to be checkpointed after
every tool round and resumed from an arbitrary mid-conversation state, and the crash hook needs
a seam between a tool result and the next request. (b) The Claude Agent SDK: a full coding
harness with filesystem tools; the wrong shape for a four-tool SQL analyst.

**Why this.** The manual loop is about forty lines and gives explicit control points for the
heartbeat checkpoint, the crash hook, the stable tool ordering that prompt caching needs, and
the final-answer schema via structured outputs. It stays on the Messages API with tool use, the
Claude-native primitive, and would collapse back to the tool runner if a future SDK version
exposed per-turn hooks with resumable message state.
