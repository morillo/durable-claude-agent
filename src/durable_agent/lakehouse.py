"""DuckDB over Delta Lake: the agent's only path to data.

Design
------
* **In-memory DuckDB, views over ``delta_scan``.** No DuckDB database file exists, so there is
  nothing to write to. Each Delta table directory becomes a view with the same name, and the
  views show up in ``information_schema`` like ordinary tables, which is what the schema tool
  introspects.
* **Read-only by construction.** Delta views cannot be written through DuckDB's view layer,
  and the connection locks its configuration after setup so a query cannot flip settings.
  The deterministic risk classifier blocks writes before they get here anyway; this is the
  second line of defense.
* **Bounded execution.** Every query is wrapped as ``SELECT * FROM (<sql>) LIMIT max_rows+1``
  so truncation is detected, and a watchdog thread calls ``interrupt()`` at the timeout.
* **PII-aware schema descriptions.** Sample rows for columns the policy marks as PII are
  redacted before they reach a prompt.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb


class LakehouseError(RuntimeError):
    """Base class for lakehouse failures surfaced to callers."""


class QueryTimeoutError(LakehouseError):
    """Raised when a query exceeds its wall-clock budget and is interrupted."""


@dataclass(frozen=True)
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    elapsed_ms: int
    sql: str

    def to_records(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, row, strict=True)) for row in self.rows]


@dataclass
class Lakehouse:
    """A DuckDB connection with one view per Delta table found under ``path``."""

    path: Path
    con: duckdb.DuckDBPyConnection = field(init=False, repr=False)
    tables: list[str] = field(init=False, default_factory=list)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        if not self.path.is_dir():
            raise LakehouseError(f"Lakehouse path does not exist: {self.path}. Run `make seed`.")
        self.con = duckdb.connect(database=":memory:")
        self.con.execute("INSTALL delta; LOAD delta;")
        for table_dir in sorted(p for p in self.path.iterdir() if (p / "_delta_log").is_dir()):
            name = table_dir.name
            # Identifier is quoted; path goes in as a string literal via parameter-free SQL
            # built from a path we own (not user input).
            uri = table_dir.resolve().as_posix().replace("'", "''")
            self.con.execute(f"CREATE VIEW \"{name}\" AS SELECT * FROM delta_scan('{uri}')")
            self.tables.append(name)
        if not self.tables:
            raise LakehouseError(f"No Delta tables found under {self.path}. Run `make seed`.")
        # Freeze configuration so a query cannot SET anything (e.g. enable external access).
        self.con.execute("SET lock_configuration = true")

    # -- schema ---------------------------------------------------------------------------

    def schema(self) -> dict[str, dict[str, str]]:
        """``{table: {column: DUCKDB_TYPE}}`` for every view, in column order."""
        rows = self.con.execute(
            """
            SELECT table_name, column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = 'main'
            ORDER BY table_name, ordinal_position
            """
        ).fetchall()
        out: dict[str, dict[str, str]] = {}
        for table, column, dtype in rows:
            out.setdefault(table, {})[column] = dtype
        return out

    def row_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for t in self.tables:
            row = self.con.execute(f'SELECT count(*) FROM "{t}"').fetchone()
            counts[t] = int(row[0]) if row else 0
        return counts

    def describe_schema(
        self,
        *,
        exclude: Collection[str] = (),
        mask_columns: Collection[str] = (),
        sample_rows: int = 2,
    ) -> str:
        """Compact DDL-style text for prompts: one block per table plus redacted samples.

        ``exclude`` hides restricted tables entirely. ``mask_columns`` takes ``table.column``
        strings whose sample values are replaced with ``<redacted>``.
        """
        masked = set(mask_columns)
        parts: list[str] = []
        for table, cols in self.schema().items():
            if table in exclude:
                continue
            col_lines = ",\n".join(f"  {c} {t}" for c, t in cols.items())
            parts.append(f"CREATE TABLE {table} (\n{col_lines}\n);")
            if sample_rows > 0:
                sample = self.con.execute(
                    f'SELECT * FROM "{table}" LIMIT {int(sample_rows)}'
                ).fetchall()
                parts.append(f"-- sample rows from {table}:")
                names = list(cols)
                for row in sample:
                    values = [
                        "<redacted>" if f"{table}.{n}" in masked else _fmt(v)
                        for n, v in zip(names, row, strict=True)
                    ]
                    parts.append("--   " + ", ".join(values))
            parts.append("")
        return "\n".join(parts).rstrip() + "\n"

    # -- execution ------------------------------------------------------------------------

    def explain(self, sql: str) -> str:
        """Plan the query without running it. Raises ``duckdb.Error`` on invalid SQL."""
        plan = self.con.execute(f"EXPLAIN {_strip(sql)}").fetchall()
        return "\n".join(str(line) for _, line in plan)

    def execute(self, sql: str, *, max_rows: int, timeout_s: float) -> QueryResult:
        """Run a read query with a hard row cap and a wall-clock timeout."""
        wrapped = f"SELECT * FROM ({_strip(sql)}) AS __q LIMIT {int(max_rows) + 1}"
        start = time.perf_counter()
        with self._watchdog(timeout_s):
            try:
                cur = self.con.execute(wrapped)
                rows = cur.fetchall()
                columns = [d[0] for d in cur.description or []]
            except duckdb.InterruptException as e:
                raise QueryTimeoutError(f"query exceeded {timeout_s}s and was interrupted") from e
        elapsed = int((time.perf_counter() - start) * 1000)
        truncated = len(rows) > max_rows
        rows = rows[:max_rows]
        return QueryResult(
            columns=columns,
            rows=[list(r) for r in rows],
            row_count=len(rows),
            truncated=truncated,
            elapsed_ms=elapsed,
            sql=sql,
        )

    @contextmanager
    def _watchdog(self, timeout_s: float) -> Iterator[None]:
        timer = threading.Timer(timeout_s, self.con.interrupt)
        timer.daemon = True
        timer.start()
        try:
            yield
        finally:
            timer.cancel()

    def close(self) -> None:
        self.con.close()


def _strip(sql: str) -> str:
    return sql.strip().rstrip(";").strip()


def _fmt(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, str):
        return repr(v)
    return str(v)
