# ADR-004: Deterministic sqlglot risk classification as the only gate signal

**Decision.** `risk.py` parses the generated SQL with sqlglot, resolves columns against the
live schema, and applies the rules in `policies.md` (whose lists it loads from an embedded TOML
block). The model's self-reported risk is recorded but never used for control flow.

**Alternatives.** (a) Ask the model to classify its own SQL: it is usually right, and in the
baseline it agreed with the classifier every time, but "usually" is not a security property and
the classification could be steered by the question text. (b) Keyword or regex guards (the
approach in morillo/text-to-sql): fast, but `SELECT *` on a PII table passes, a CTE hides a
restricted table, and `read_csv('/etc/passwd')` is not a keyword. (c) Database-level grants
only: necessary in production, but they do not express "needs human approval" and give the
model no actionable feedback.

**Why this.** An AST with schema-qualified columns sees what the query touches regardless of
how it is written; the rules are unit-tested exhaustively (every rule, positive and negative
cases) and documented in prose next to the machine-readable block; and the same classifier runs
in `validate_sql` so the model gets the verdict before finishing and can fix what it can (add a
LIMIT, drop a PII column). Conservative by design: a CTE that selects `*` from `customers` is
`needs_approval` even if only a count leaves.
