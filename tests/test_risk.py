"""Exhaustive tests for the deterministic risk classifier.

Every rule has positive and negative cases, run against the real seeded schema and the real
``policies.md`` so a doc edit that breaks the classifier fails here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from durable_agent.risk import Policy, RiskLevel, classify, load_policy

Schema = dict[str, dict[str, str]]

# ---------------------------------------------------------------------------------------------
# Policy loading and doc/code sync
# ---------------------------------------------------------------------------------------------


def test_policy_loads_from_markdown(policy: Policy) -> None:
    assert policy.max_rows_without_approval == 1000
    assert "payment_cards" in policy.restricted_tables
    assert {"customers.email", "customers.phone", "customers.full_name"} <= policy.pii_columns
    assert "read_csv" in policy.blocked_functions


def test_policy_names_exist_in_schema(policy: Policy, schema: Schema) -> None:
    for t in policy.restricted_tables:
        assert t in schema, f"restricted table {t} missing from lakehouse"
    for col in policy.pii_columns:
        t, c = col.split(".")
        assert c in schema[t], f"PII column {col} missing from lakehouse"


def test_policy_missing_block_is_an_error(tmp_path: Path) -> None:
    p = tmp_path / "policies.md"
    p.write_text("# no toml here\n")
    with pytest.raises(ValueError, match="toml"):
        load_policy(p)


# ---------------------------------------------------------------------------------------------
# SAFE
# ---------------------------------------------------------------------------------------------

SAFE = [
    "SELECT sum(gross_amount) FROM orders",
    "SELECT count(*) FROM orders WHERE status <> 'cancelled'",
    "SELECT sum(gross_amount - discount_amount - refund_amount) AS net FROM orders WHERE status <> 'cancelled'",
    "SELECT country, count(*) AS n FROM customers GROUP BY country ORDER BY n DESC LIMIT 10",
    "SELECT c.country, sum(o.gross_amount) FROM customers c JOIN orders o ON o.customer_id = c.customer_id GROUP BY 1 LIMIT 100",
    "WITH m AS (SELECT date_trunc('month', order_date) AS m, sum(gross_amount) s FROM orders GROUP BY 1) SELECT * FROM m ORDER BY m LIMIT 24",
    "SELECT p.category, sum(i.quantity) FROM order_items i JOIN products p USING (product_id) GROUP BY 1 LIMIT 50",
    "SELECT order_id, gross_amount, rank() OVER (ORDER BY gross_amount DESC) FROM orders LIMIT 5",
    "SELECT country FROM customers UNION SELECT ship_country FROM orders LIMIT 20",
    "FROM orders SELECT count(*)",
    "SELECT count(*) FROM orders LIMIT 1",
    "select avg(gross_amount) from orders where order_year = 2025",
    "SELECT customer_id, count(*) FROM orders GROUP BY customer_id ORDER BY 2 DESC LIMIT 1000",
    "SELECT count(DISTINCT customer_id) FROM orders",
]


@pytest.mark.parametrize("sql", SAFE)
def test_safe(sql: str, schema: Schema, policy: Policy) -> None:
    a = classify(sql, schema, policy)
    assert a.level == RiskLevel.SAFE, a.reasons
    assert a.reasons == ()


# ---------------------------------------------------------------------------------------------
# NEEDS_APPROVAL
# ---------------------------------------------------------------------------------------------

NEEDS_APPROVAL = [
    # R4: PII
    ("SELECT email FROM customers LIMIT 5", "R4"),
    ("SELECT * FROM customers LIMIT 5", "R4"),
    ("SELECT c.customer_id FROM customers c WHERE c.email LIKE '%@example.com' LIMIT 5", "R4"),
    (
        "SELECT customers.full_name, count(*) FROM customers JOIN orders USING (customer_id) GROUP BY 1 LIMIT 10",
        "R4",
    ),
    ("SELECT phone FROM customers WHERE country = 'US' LIMIT 1", "R4"),
    ("SELECT customer_id FROM customers ORDER BY email LIMIT 3", "R4"),
    ("SELECT * FROM (SELECT email FROM customers) sub LIMIT 10", "R4"),
    ("WITH c AS (SELECT * FROM customers) SELECT count(*) FROM c", "R4"),
    ("SELECT count(DISTINCT email) FROM customers", "R4"),
    # R6: unbounded
    ("SELECT country, count(*) FROM customers GROUP BY country", "R6"),
    ("SELECT order_id FROM orders", "R6"),
    ("SELECT order_id FROM orders LIMIT 1001", "R6"),
    ("SELECT country FROM customers UNION SELECT ship_country FROM orders", "R6"),
    ("SELECT order_id, sum(gross_amount) OVER () FROM orders", "R6"),
    ("SELECT DISTINCT country FROM customers", "R6"),
    ("SELECT order_id FROM orders LIMIT (SELECT 5)", "R6"),
]


@pytest.mark.parametrize(("sql", "rule"), NEEDS_APPROVAL)
def test_needs_approval(sql: str, rule: str, schema: Schema, policy: Policy) -> None:
    a = classify(sql, schema, policy)
    assert a.level == RiskLevel.NEEDS_APPROVAL, a.reasons
    assert rule in {r.rule for r in a.reasons}


def test_pii_reason_lists_columns_and_section(schema: Schema, policy: Policy) -> None:
    a = classify("SELECT * FROM customers LIMIT 1", schema, policy)
    (r,) = [r for r in a.reasons if r.rule == "R4"]
    assert "customers.email" in r.message and "customers.phone" in r.message
    assert r.section == "Classification of data > PII columns"
    assert a.sections == ("Classification of data > PII columns",)


# ---------------------------------------------------------------------------------------------
# BLOCKED
# ---------------------------------------------------------------------------------------------

BLOCKED = [
    # R1: not a read-only query
    ("DELETE FROM orders WHERE 1=1", "R1"),
    ("UPDATE orders SET gross_amount = 0", "R1"),
    ("INSERT INTO orders VALUES (1)", "R1"),
    ("DROP TABLE orders", "R1"),
    ("CREATE TABLE t AS SELECT 1", "R1"),
    ("ALTER TABLE orders ADD COLUMN x INT", "R1"),
    ("TRUNCATE orders", "R1"),
    ("COPY orders TO '/tmp/o.csv'", "R1"),
    ("ATTACH 'x.db' AS y", "R1"),
    ("INSTALL httpfs", "R1"),
    ("LOAD httpfs", "R1"),
    ("SET enable_external_access = true", "R1"),
    ("PRAGMA database_list", "R1"),
    ("SELCT nonsense", "R1"),
    ("", "R1"),
    ("   ;  ", "R1"),
    # R2: multiple statements
    ("SELECT count(*) FROM orders; SELECT 1", "R2"),
    ("SELECT count(*) FROM orders; DROP TABLE orders", "R2"),
    # R3: restricted tables
    ("SELECT count(*) FROM payment_cards", "R3"),
    ("SELECT c.country FROM customers c JOIN payment_cards p USING (customer_id) LIMIT 5", "R3"),
    ("WITH pc AS (SELECT * FROM payment_cards) SELECT count(*) FROM pc", "R3"),
    ("SELECT count(*) FROM main.payment_cards", "R3"),
    # R7: outside the catalog
    ("SELECT * FROM nonexistent LIMIT 5", "R7"),
    ("SELECT * FROM information_schema.tables LIMIT 5", "R7"),
    ("SELECT * FROM duckdb_settings() LIMIT 5", "R8"),
    # R8: external functions
    ("SELECT * FROM read_csv('/etc/passwd') LIMIT 5", "R8"),
    ("SELECT * FROM read_parquet('s3://bucket/x.parquet') LIMIT 5", "R8"),
    ("SELECT * FROM delta_scan('./data/lakehouse/payment_cards') LIMIT 5", "R8"),
    ("SELECT getenv('ANTHROPIC_API_KEY')", "R8"),
    ("SELECT count(*) FROM orders WHERE gross_amount > (SELECT count(*) FROM read_csv('x'))", "R8"),
]


@pytest.mark.parametrize(("sql", "rule"), BLOCKED)
def test_blocked(sql: str, rule: str, schema: Schema, policy: Policy) -> None:
    a = classify(sql, schema, policy)
    assert a.level == RiskLevel.BLOCKED, a.reasons
    assert rule in {r.rule for r in a.reasons}


def test_blocked_dominates_needs_approval(schema: Schema, policy: Policy) -> None:
    a = classify(
        "SELECT email FROM customers JOIN payment_cards USING (customer_id)", schema, policy
    )
    assert a.level == RiskLevel.BLOCKED
    assert {r.rule for r in a.reasons} == {"R3", "R4", "R6"}


# ---------------------------------------------------------------------------------------------
# Metadata used downstream
# ---------------------------------------------------------------------------------------------


def test_assessment_metadata_and_serialization(schema: Schema, policy: Policy) -> None:
    sql = "SELECT c.country, sum(o.gross_amount) FROM customers c JOIN orders o ON o.customer_id = c.customer_id GROUP BY 1 LIMIT 10"
    a = classify(sql, schema, policy)
    assert a.tables == ("customers", "orders")
    assert set(a.columns) == {
        "customers.country",
        "customers.customer_id",
        "orders.customer_id",
        "orders.gross_amount",
    }
    assert a.limit == 10 and a.scalar_aggregate is False
    d = a.to_dict()
    assert d["level"] == "safe" and d["reasons"] == [] and d["sections"] == []


def test_never_raises_on_garbage(schema: Schema, policy: Policy) -> None:
    for sql in ["((((", "SELECT FROM WHERE", "\x00", "SELECT 'unterminated", "-- just a comment"]:
        a = classify(sql, schema, policy)
        assert a.level == RiskLevel.BLOCKED, sql


def test_case_insensitive_names(schema: Schema, policy: Policy) -> None:
    assert (
        classify("SELECT EMAIL FROM Customers LIMIT 1", schema, policy).level
        == RiskLevel.NEEDS_APPROVAL
    )
    assert classify("SELECT COUNT(*) FROM PAYMENT_CARDS", schema, policy).level == RiskLevel.BLOCKED
