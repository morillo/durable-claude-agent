# Governance Policies — Analyst Agent

These rules govern every SQL statement the analyst agent proposes. They are enforced by
deterministic code (`risk.py`) that parses the SQL, never by trusting the model's own
assessment. Each heading is a citable section.

## Classification of data

### PII columns

The following columns are personal data under the company's privacy policy. Any query that
selects, filters, groups, or joins on them requires human approval before execution, and the
result is treated as confidential.

- `customers.full_name`
- `customers.email`
- `customers.phone`

`SELECT *` on `customers` touches every PII column and therefore requires approval. Prefer
listing non-PII columns explicitly.

### Restricted tables

The following tables may never be read by the analyst agent, for any requester. Queries that
reference them are blocked outright and are not eligible for approval.

- `payment_cards`

## Query shape rules

### Read-only

Only a single read-only query is allowed: a `SELECT` (including CTEs, joins, set operations,
and window functions). Any statement that writes, alters, or configures is blocked:
`INSERT`, `UPDATE`, `DELETE`, `MERGE`, `DROP`, `CREATE`, `ALTER`, `TRUNCATE`, `COPY`,
`ATTACH`, `INSTALL`, `LOAD`, `SET`, `PRAGMA`. Multiple statements in one request are blocked.

### Catalog-only access

Queries may reference only the governed tables listed in the data dictionary. Table-valued
functions that read files or external systems (`read_csv`, `read_parquet`, `read_json`,
`delta_scan`, `iceberg_scan`, `glob`, and similar) bypass governance and are blocked. System
catalogs such as `information_schema` are not queryable through the agent; use the schema
tool instead.

### Row limits

Result sets are capped at **1000 rows**. A query must include `LIMIT` of at most 1000 unless
it returns a single aggregate row (no `GROUP BY`, no window functions). Queries without a
`LIMIT`, or with a `LIMIT` above 1000, require approval.

### Full-table scans of PII tables

Scanning `customers` without a filter and returning PII columns is the highest-risk approved
shape. It requires approval and a business justification in the request note.

## Risk classes and approval

| Risk class | Meaning | What happens |
|---|---|---|
| `safe` | No rule triggered. | Executes automatically. |
| `needs_approval` | PII columns touched, or result size unbounded. | Workflow pauses for a human decision; times out after 24 hours. |
| `blocked` | Writes, restricted tables, external access, unparseable or multi-statement SQL. | Workflow ends with the reason. Never executes, never eligible for approval. |

Approvers must be a member of the data-governance group. The approval decision and note are
recorded in the workflow history.

## Machine-readable policy

The classifier loads this block at startup. Edit the lists above and this block together.

```toml
[policy]
max_rows_without_approval = 1000
restricted_tables = ["payment_cards"]
pii_columns = ["customers.full_name", "customers.email", "customers.phone"]
blocked_functions = [
  "read_csv", "read_csv_auto", "read_parquet", "parquet_scan", "read_json", "read_json_auto",
  "read_json_objects", "read_text", "read_blob", "glob", "delta_scan", "iceberg_scan",
  "getenv",
]
```
