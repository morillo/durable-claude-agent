# ADR-005: DuckDB over Delta Lake tables as the zero-infrastructure lakehouse

**Decision.** Seed data is written as Delta tables with delta-rs; DuckDB reads them through
its `delta` extension as views; all queries run in DuckDB.

**Alternatives.** (a) SQLite with the Chinook database (the text-to-sql repo's choice): trivial
to set up, but it tells no lakehouse story and has no partitioning, no table format, no path to
Spark or Databricks. (b) Spark or Trino locally: faithful, but minutes of JVM startup and
gigabytes of dependencies for a demo. (c) Parquet files without a table format: nearly as
simple, but loses the transaction log, time travel, and the "same files readable by any engine"
claim.

**Why this.** Delta tables are the artifact an enterprise actually has; DuckDB reads them in
milliseconds on a laptop and exposes `information_schema`, `EXPLAIN`, and `interrupt()`, which
the schema tool, the dry-run tool, and the statement timeout need. The dataset is synthetic and
deterministic so the eval gold rows are stable, and it contains deliberate governance traps
(cancelled orders keep a gross amount, refunds in their own column, shipping is not revenue).
Swapping in Unity Catalog, an Iceberg REST catalog, or Snowflake changes `lakehouse.py` and
nothing above it.
