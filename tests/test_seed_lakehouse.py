"""Seed determinism and integrity; lakehouse schema, masking, limits, and timeouts."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from durable_agent.lakehouse import Lakehouse, LakehouseError, QueryTimeoutError
from durable_agent.risk import Policy
from durable_agent.seed import SCHEMAS, SeedSpec, generate

EXPECTED_TABLES = {"customers", "products", "orders", "order_items", "payment_cards"}


def test_generate_is_deterministic() -> None:
    a = generate(SeedSpec().scaled(0.05))
    b = generate(SeedSpec().scaled(0.05))
    for name in EXPECTED_TABLES:
        assert a[name].equals(b[name]), name


def test_generate_matches_declared_schemas() -> None:
    tables = generate(SeedSpec().scaled(0.05))
    for name, table in tables.items():
        assert table.schema.equals(SCHEMAS[name]), name


def test_manifest_and_views(lakehouse: Lakehouse, lakehouse_path: Path) -> None:
    manifest = json.loads((lakehouse_path / "MANIFEST.json").read_text())
    assert set(lakehouse.tables) == EXPECTED_TABLES
    assert lakehouse.row_counts() == manifest["row_counts"]
    assert sum(manifest["row_counts"].values()) > 500


def test_referential_integrity(lakehouse: Lakehouse) -> None:
    con = lakehouse.con
    orphans = con.execute(
        "SELECT count(*) FROM orders o LEFT JOIN customers c USING (customer_id) WHERE c.customer_id IS NULL"
    ).fetchone()
    assert orphans is not None and orphans[0] == 0
    orphans = con.execute(
        "SELECT count(*) FROM order_items i LEFT JOIN orders o USING (order_id) WHERE o.order_id IS NULL"
    ).fetchone()
    assert orphans is not None and orphans[0] == 0
    orphans = con.execute(
        "SELECT count(*) FROM order_items i LEFT JOIN products p USING (product_id) WHERE p.product_id IS NULL"
    ).fetchone()
    assert orphans is not None and orphans[0] == 0


def test_gross_amount_equals_item_total(lakehouse: Lakehouse) -> None:
    bad = lakehouse.con.execute(
        """
        SELECT count(*) FROM orders o
        JOIN (SELECT order_id, round(sum(quantity * unit_price), 2) AS total
              FROM order_items GROUP BY order_id) i USING (order_id)
        WHERE abs(o.gross_amount - i.total) > 0.011
        """
    ).fetchone()
    assert bad is not None and bad[0] == 0


def test_governance_traps_exist(lakehouse: Lakehouse) -> None:
    """Cancelled orders carry gross_amount; refunds only on refunded orders; partition column."""
    con = lakehouse.con
    row = con.execute(
        "SELECT count(*), sum(gross_amount) FROM orders WHERE status='cancelled'"
    ).fetchone()
    assert row is not None and row[0] > 0 and row[1] > 0
    row = con.execute(
        "SELECT count(*) FROM orders WHERE refund_amount > 0 AND status <> 'refunded'"
    ).fetchone()
    assert row is not None and row[0] == 0
    row = con.execute("SELECT count(*) FROM orders WHERE order_year <> year(order_date)").fetchone()
    assert row is not None and row[0] == 0


def test_schema_columns_match_dictionary(schema: dict[str, dict[str, str]]) -> None:
    assert set(schema) == EXPECTED_TABLES
    assert list(schema["orders"])[:3] == ["order_id", "customer_id", "order_date"]
    assert schema["orders"]["order_date"] == "DATE"
    assert schema["orders"]["order_ts"] == "TIMESTAMP"
    assert schema["customers"]["marketing_opt_in"] == "BOOLEAN"


def test_describe_schema_excludes_and_masks(lakehouse: Lakehouse, policy: Policy) -> None:
    text = lakehouse.describe_schema(
        exclude=policy.restricted_tables, mask_columns=policy.pii_columns
    )
    assert "CREATE TABLE customers" in text
    assert "payment_cards" not in text
    assert "<redacted>" in text
    assert "@example.com" not in text  # no sample email leaks into the prompt
    assert "-- sample rows from orders:" in text


def test_execute_truncates_and_reports(lakehouse: Lakehouse) -> None:
    res = lakehouse.execute(
        "SELECT order_id FROM orders ORDER BY order_id", max_rows=10, timeout_s=5
    )
    assert res.columns == ["order_id"]
    assert res.row_count == 10 and res.truncated is True
    assert res.rows[0] == [1]
    res = lakehouse.execute("SELECT count(*) AS n FROM orders;", max_rows=10, timeout_s=5)
    assert res.truncated is False and res.row_count == 1
    assert res.to_records()[0]["n"] > 0


def test_execute_timeout_interrupts(lakehouse: Lakehouse) -> None:
    heavy = "SELECT sum(a.range * b.range) FROM range(200000000) a, range(50) b"
    with pytest.raises(QueryTimeoutError):
        lakehouse.execute(heavy, max_rows=10, timeout_s=0.2)
    # The connection is still usable afterwards.
    assert lakehouse.execute("SELECT 1 AS x", max_rows=1, timeout_s=5).rows == [[1]]


def test_explain_validates_without_running(lakehouse: Lakehouse) -> None:
    plan = lakehouse.explain("SELECT count(*) FROM orders")
    assert "AGGREGATE" in plan.upper()
    with pytest.raises(duckdb.Error):
        lakehouse.explain("SELECT nope FROM orders")


def test_configuration_is_locked(lakehouse: Lakehouse) -> None:
    with pytest.raises(duckdb.Error):
        lakehouse.con.execute("SET enable_external_access = true")


def test_missing_path_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(LakehouseError):
        Lakehouse(tmp_path / "nope")
    (tmp_path / "empty").mkdir()
    with pytest.raises(LakehouseError):
        Lakehouse(tmp_path / "empty")
