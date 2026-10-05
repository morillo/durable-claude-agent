# ADR-002: MCP tools over Streamable HTTP, `run_sql` gated by a bearer capability

**Decision.** The four tools are served by one MCP server over Streamable HTTP. The model-facing
session sees only `get_schema`, `search_governance`, `validate_sql`. `run_sql` checks an
`Authorization: Bearer` header in constant time against a secret only the worker holds, and
re-runs the deterministic classifier before executing.

**Alternatives.** (a) Plain Python functions passed to the model as tools: simplest, but the
tool surface would be bound to this process and the "orchestrator-only tool" would be a
convention, not a boundary. (b) MCP over stdio: standard for local tools, but stdio carries no
request headers, so the capability would have to be a tool argument visible in the schema the
model reads. (c) Trusting a system-prompt instruction not to call `run_sql`: a request, not a
control.

**Why this.** HTTP transport gives a real credential channel (`ctx.headers`) the model never
sees; the MCP protocol keeps the tool server independently deployable and inspectable; and the
two controls (filtered tool list, server-side token check) are independent so both must fail
before the model can execute anything. The classifier re-run inside `run_sql` is defense in
depth against a compromised or buggy caller holding the token.
