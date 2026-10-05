# ADR-006: Haiku for planning and summary, Sonnet for SQL and judging, effort as the dial

**Decision.** `plan_query` and `summarize` use `claude-haiku-4-5`; `generate_sql` and the eval
judge use `claude-sonnet-5-5` at `effort=medium` (judge at `low`). Both are env-configurable.

**Alternatives.** (a) One strong model everywhere: simplest and most accurate, about 3x the
cost per run for steps where Haiku is already correct. (b) Opus for SQL: the current default
recommendation for agentic work, but this task is narrow and Sonnet scored 96% execution
accuracy; the headroom is in retrieval, not reasoning. (c) Disabling thinking to save tokens:
not possible on Sonnet 5.5 and counterproductive on current models; effort is the supported
control.

**Why this.** The two cheap steps are structured-output transformations over supplied context,
which Haiku handles reliably; SQL generation with tools is where errors cost money and where
Sonnet's tool use earns its price. Structured outputs replace prefill (rejected by current
models), refusal fallbacks are on by default for Sonnet, and a per-call price table turns
`usage` into dollars so every run and every scorecard reports cost. Changing the routing is one
environment variable and one eval run to see the effect.
