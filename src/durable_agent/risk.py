"""Deterministic SQL risk classification against ``governance/policies.md``.

Why this is code and not a prompt
---------------------------------
The model in ``generate_sql`` self-reports a risk flag, and that flag is recorded, but it is
never trusted for control flow. This module parses the SQL into an AST with sqlglot, resolves
every column to its source table using the live schema, and applies the policy rules
mechanically. The same input always yields the same class, the rules are unit-tested
exhaustively, and a reviewer can read the rule list without reading a transcript.

Rule catalogue (the ``section`` of each reason is a heading in ``policies.md``)
-------------------------------------------------------------------------------
R1  not a single read-only query (DML/DDL/config, or unparseable)        -> blocked
R2  more than one statement                                               -> blocked
R3  references a restricted table                                         -> blocked
R4  touches a PII column (select, filter, join, group)                    -> needs_approval
R6  unbounded result: no LIMIT, or LIMIT above the cap, unless scalar agg -> needs_approval
R7  references a table outside the governed catalog                       -> blocked
R8  table-valued or scalar function that reaches outside the catalog      -> blocked

The policy lists themselves (PII columns, restricted tables, row cap, blocked functions) are
loaded from the TOML block at the bottom of ``policies.md``, so the document stays the single
source of truth.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify

DIALECT = "duckdb"
DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[2] / "governance" / "policies.md"

_TOML_BLOCK = re.compile(r"```toml\s*\n(.*?)\n```", re.DOTALL)


class RiskLevel(StrEnum):
    SAFE = "safe"
    NEEDS_APPROVAL = "needs_approval"
    BLOCKED = "blocked"


_SEVERITY = {RiskLevel.SAFE: 0, RiskLevel.NEEDS_APPROVAL: 1, RiskLevel.BLOCKED: 2}

# Section headings in policies.md that each rule cites.
SECTIONS = {
    "R1": "Query shape rules > Read-only",
    "R2": "Query shape rules > Read-only",
    "R3": "Classification of data > Restricted tables",
    "R4": "Classification of data > PII columns",
    "R6": "Query shape rules > Row limits",
    "R7": "Query shape rules > Catalog-only access",
    "R8": "Query shape rules > Catalog-only access",
}


@dataclass(frozen=True)
class Policy:
    max_rows_without_approval: int
    restricted_tables: frozenset[str]
    pii_columns: frozenset[str]  # "table.column"
    blocked_functions: frozenset[str]

    @classmethod
    def from_markdown(cls, path: Path = DEFAULT_POLICY_PATH) -> Policy:
        """Parse the ```toml``` block embedded in ``policies.md``."""
        text = Path(path).read_text(encoding="utf-8")
        match = _TOML_BLOCK.search(text)
        if not match:
            raise ValueError(f"No ```toml policy block found in {path}")
        data = tomllib.loads(match.group(1))["policy"]
        return cls(
            max_rows_without_approval=int(data["max_rows_without_approval"]),
            restricted_tables=frozenset(t.lower() for t in data["restricted_tables"]),
            pii_columns=frozenset(c.lower() for c in data["pii_columns"]),
            blocked_functions=frozenset(f.lower() for f in data["blocked_functions"]),
        )


def load_policy(path: Path | None = None) -> Policy:
    return Policy.from_markdown(path or DEFAULT_POLICY_PATH)


@dataclass(frozen=True)
class RiskReason:
    rule: str
    level: RiskLevel
    message: str
    section: str


@dataclass(frozen=True)
class RiskAssessment:
    level: RiskLevel
    reasons: tuple[RiskReason, ...]
    tables: tuple[str, ...]
    columns: tuple[str, ...]
    limit: int | None
    scalar_aggregate: bool

    @property
    def sections(self) -> tuple[str, ...]:
        """Distinct policy sections cited, in rule order (for answer citations)."""
        seen: dict[str, None] = {}
        for r in self.reasons:
            seen.setdefault(r.section)
        return tuple(seen)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["level"] = self.level.value
        d["reasons"] = [{**asdict(r), "level": r.level.value} for r in self.reasons]
        d["sections"] = list(self.sections)
        return d


def classify(
    sql: str,
    schema: Mapping[str, Mapping[str, str]],
    policy: Policy,
) -> RiskAssessment:
    """Classify one SQL string. Never raises on bad SQL; bad SQL is ``blocked``."""
    reasons: list[RiskReason] = []
    schema_lc: dict[str, Any] = {
        t.lower(): {c.lower(): ty for c, ty in cols.items()} for t, cols in schema.items()
    }

    def add(rule: str, level: RiskLevel, message: str) -> None:
        reasons.append(RiskReason(rule, level, message, SECTIONS[rule]))

    # -- R1/R2: parse and shape -------------------------------------------------------------
    try:
        statements = [s for s in sqlglot.parse(sql, read=DIALECT) if s is not None]
    except sqlglot.errors.SqlglotError as e:
        add("R1", RiskLevel.BLOCKED, f"SQL could not be parsed: {str(e).splitlines()[0]}")
        return _finish(reasons, (), (), None, False)

    if not statements:
        add("R1", RiskLevel.BLOCKED, "empty statement")
        return _finish(reasons, (), (), None, False)
    if len(statements) > 1:
        add(
            "R2",
            RiskLevel.BLOCKED,
            f"{len(statements)} statements supplied; exactly one is allowed",
        )
    stmt = statements[0]
    if not isinstance(stmt, exp.Query):
        add(
            "R1",
            RiskLevel.BLOCKED,
            f"statement type {type(stmt).__name__.upper()} is not a read-only query",
        )
        return _finish(reasons, (), (), None, False)

    # -- resolve names ----------------------------------------------------------------------
    try:
        q = qualify(stmt.copy(), schema=schema_lc, dialect=DIALECT, validate_qualify_columns=False)
    except Exception:
        q = stmt

    cte_names = {c.alias.lower() for c in q.find_all(exp.CTE)}
    derived = {s.alias.lower() for s in q.find_all(exp.Subquery) if s.alias}

    alias_to_table: dict[str, str] = {}
    tables: set[str] = set()
    for t in q.find_all(exp.Table):
        # -- R8: table-valued functions (read_csv, delta_scan, ...) ----------------------
        if isinstance(t.this, exp.Func):
            add(
                "R8",
                RiskLevel.BLOCKED,
                f"table function {_func_name(t.this)}() reaches outside the catalog",
            )
            continue
        name = t.name.lower()
        if not name or name in cte_names:
            continue
        qualified = f"{t.db.lower()}.{name}" if t.db else name
        alias_to_table[(t.alias or t.name).lower()] = name
        tables.add(qualified)
        # -- R3/R7: restricted or unknown ---------------------------------------------
        if name in policy.restricted_tables:
            add("R3", RiskLevel.BLOCKED, f"table {name} is restricted")
        elif t.db or name not in schema_lc:
            add("R7", RiskLevel.BLOCKED, f"table {qualified} is not in the governed catalog")

    # -- R8: scalar functions --------------------------------------------------------------
    for f in q.find_all(exp.Func):
        fname = _func_name(f)
        if fname in policy.blocked_functions and not isinstance(f.parent, exp.Table):
            add("R8", RiskLevel.BLOCKED, f"function {fname}() reaches outside the catalog")

    # -- R4: PII columns -------------------------------------------------------------------
    columns: set[str] = set()
    for c in q.find_all(exp.Column):
        owner = c.table.lower()
        if owner in cte_names or owner in derived:
            continue  # inner query already contributed the real column
        real = alias_to_table.get(owner, owner)
        if real in schema_lc and c.name.lower() in schema_lc[real]:
            columns.add(f"{real}.{c.name.lower()}")
    pii_hit = sorted(columns & policy.pii_columns)
    if pii_hit:
        add("R4", RiskLevel.NEEDS_APPROVAL, f"touches PII column(s): {', '.join(pii_hit)}")

    # -- R6: row bound ----------------------------------------------------------------------
    limit = _limit_value(q)
    scalar = _is_scalar_aggregate(q)
    if not scalar:
        cap = policy.max_rows_without_approval
        if limit is None:
            add(
                "R6",
                RiskLevel.NEEDS_APPROVAL,
                f"no LIMIT on a non-scalar query (cap is {cap} rows)",
            )
        elif limit > cap:
            add("R6", RiskLevel.NEEDS_APPROVAL, f"LIMIT {limit} exceeds the {cap}-row cap")

    return _finish(reasons, tuple(sorted(tables)), tuple(sorted(columns)), limit, scalar)


# -- helpers ----------------------------------------------------------------------------------


def _finish(
    reasons: list[RiskReason],
    tables: tuple[str, ...],
    columns: tuple[str, ...],
    limit: int | None,
    scalar: bool,
) -> RiskAssessment:
    level = max((r.level for r in reasons), key=_SEVERITY.__getitem__, default=RiskLevel.SAFE)
    return RiskAssessment(level, tuple(reasons), tables, columns, limit, scalar)


def _func_name(f: exp.Expr) -> str:
    if isinstance(f, exp.Anonymous):
        return f.name.lower()
    return f.sql_name().lower() if isinstance(f, exp.Func) else type(f).__name__.lower()


def _limit_value(q: exp.Expr) -> int | None:
    lim = q.args.get("limit")
    if lim is None:
        return None
    value = lim.expression
    if isinstance(value, exp.Literal) and value.is_int:
        return int(value.this)
    return None  # non-literal limit: treat as absent (conservative)


def _is_scalar_aggregate(q: exp.Expr) -> bool:
    """True when the top-level query returns exactly one aggregate row."""
    if not isinstance(q, exp.Select) or q.args.get("group") or q.args.get("distinct"):
        return False
    if not q.expressions:
        return False
    for proj in q.expressions:
        if isinstance(proj, exp.Star):
            return False
        aggs = [
            a
            for a in proj.find_all(exp.AggFunc)
            if a.find_ancestor(exp.Window) is None and a.find_ancestor(exp.Subquery) is None
        ]
        if not aggs:
            return False
    return True
