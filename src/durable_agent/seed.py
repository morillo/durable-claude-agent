"""Generate the synthetic retail lakehouse as Delta Lake tables.

Why synthetic, why Delta, why this shape
----------------------------------------
* **Synthetic and deterministic.** ``random.Random(SEED)`` drives every value, so two machines
  produce byte-identical tables. The eval dataset's gold rows depend on that.
* **Delta Lake via delta-rs.** ``write_deltalake`` writes real Delta transaction logs that any
  engine (Spark, Trino, DuckDB, Databricks) can read. Nothing in this repo is DuckDB-specific
  at the storage layer, which is the point of the "lakehouse" framing.
* **Governance traps on purpose.** Cancelled orders keep a non-zero ``gross_amount``, refunds
  live in a separate column, and shipping is not revenue. A model that ignores the data
  dictionary gets the wrong number. That is what the evals measure.

Run with ``make seed`` or ``python -m durable_agent.seed --out data/lakehouse``.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
from deltalake import write_deltalake

SEED = 42
DATA_START = date(2024, 1, 1)
DATA_END = date(2025, 12, 31)

COUNTRIES = ["US", "US", "US", "CA", "GB", "DE", "FR", "ES", "MX", "BR"]
US_STATES = ["CA", "NY", "TX", "WA", "IL", "FL", "MA", "CO"]
CA_PROVINCES = ["ON", "BC", "QC", "AB"]
CITIES = {
    "US": ["Seattle", "Austin", "Denver", "Boston", "Chicago", "Miami", "San Jose", "New York"],
    "CA": ["Toronto", "Vancouver", "Montreal", "Calgary"],
    "GB": ["London", "Manchester", "Leeds", "Bristol"],
    "DE": ["Berlin", "Munich", "Hamburg", "Cologne"],
    "FR": ["Paris", "Lyon", "Marseille", "Lille"],
    "ES": ["Madrid", "Barcelona", "Valencia", "Seville"],
    "MX": ["Mexico City", "Guadalajara", "Monterrey", "Puebla"],
    "BR": ["Sao Paulo", "Rio de Janeiro", "Curitiba", "Recife"],
}
FIRST_NAMES = [
    "Ava",
    "Noah",
    "Mia",
    "Liam",
    "Zoe",
    "Ethan",
    "Isla",
    "Lucas",
    "Nora",
    "Mateo",
    "Leah",
    "Owen",
    "Ivy",
    "Theo",
    "Elena",
    "Kai",
    "Maya",
    "Felix",
    "Sofia",
    "Hugo",
]
LAST_NAMES = [
    "Garcia",
    "Smith",
    "Mueller",
    "Rossi",
    "Dubois",
    "Novak",
    "Silva",
    "Khan",
    "Tanaka",
    "Nguyen",
    "Okafor",
    "Larsen",
    "Costa",
    "Walsh",
    "Ivanova",
    "Baker",
    "Moreau",
    "Haddad",
]
SEGMENTS = ["consumer"] * 7 + ["smb"] * 2 + ["enterprise"]
CATEGORIES = {
    "Electronics": ["Audio", "Wearables", "Accessories"],
    "Home": ["Kitchen", "Decor", "Lighting"],
    "Apparel": ["Outerwear", "Footwear", "Basics"],
    "Sports": ["Fitness", "Outdoor", "Cycling"],
    "Toys": ["Building", "Games", "Plush"],
    "Grocery": ["Snacks", "Beverages", "Pantry"],
    "Beauty": ["Skincare", "Haircare", "Fragrance"],
    "Office": ["Paper", "Writing", "Desk"],
}
STATUSES = (
    ["delivered"] * 70 + ["shipped"] * 10 + ["placed"] * 5 + ["cancelled"] * 7 + ["refunded"] * 8
)
CHANNELS = ["web"] * 5 + ["mobile"] * 3 + ["store"] * 2
CARD_BRANDS = ["visa", "mastercard", "amex", "discover"]


@dataclass(frozen=True)
class SeedSpec:
    """Row counts. ``scale`` shrinks everything proportionally for fast tests."""

    customers: int = 1200
    products: int = 200
    orders: int = 3000
    payment_cards: int = 1000

    def scaled(self, scale: float) -> SeedSpec:
        def f(n: int) -> int:
            return max(8, int(n * scale))

        return SeedSpec(f(self.customers), f(self.products), f(self.orders), f(self.payment_cards))


def _money(x: float) -> float:
    return round(x + 1e-9, 2)


def _customers(rng: random.Random, n: int) -> list[dict[str, Any]]:
    rows = []
    for i in range(1, n + 1):
        country = rng.choice(COUNTRIES)
        first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
        state = (
            rng.choice(US_STATES)
            if country == "US"
            else rng.choice(CA_PROVINCES)
            if country == "CA"
            else None
        )
        rows.append(
            {
                "customer_id": i,
                "full_name": f"{first} {last}",
                "email": f"{first}.{last}.{i}@example.com".lower(),
                "phone": f"+1-555-01{i % 100:02d}-{rng.randint(1000, 9999)}",
                "city": rng.choice(CITIES[country]),
                "state": state,
                "country": country,
                "segment": rng.choice(SEGMENTS),
                "signup_date": DATA_START - timedelta(days=rng.randint(0, 900)),
                "marketing_opt_in": rng.random() < 0.55,
            }
        )
    return rows


def _products(rng: random.Random, n: int) -> list[dict[str, Any]]:
    rows = []
    cats = list(CATEGORIES)
    for i in range(1, n + 1):
        category = cats[(i - 1) % len(cats)]
        sub = rng.choice(CATEGORIES[category])
        cost = _money(rng.uniform(3, 180))
        rows.append(
            {
                "product_id": i,
                "sku": f"{category[:3].upper()}-{i:05d}",
                "product_name": f"{sub} {category} Item {i}",
                "category": category,
                "subcategory": sub,
                "unit_cost": cost,
                "list_price": _money(cost * rng.uniform(1.25, 2.4)),
                "is_active": rng.random() < 0.92,
            }
        )
    return rows


def _orders_and_items(
    rng: random.Random,
    n_orders: int,
    customers: list[dict[str, Any]],
    products: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    span_days = (DATA_END - DATA_START).days
    orders: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    item_id = 1
    # A few heavy customers make "top customers" questions non-trivial.
    weights = [
        5.0 if c["segment"] == "enterprise" else 2.0 if c["segment"] == "smb" else 1.0
        for c in customers
    ]
    for order_id in range(1, n_orders + 1):
        customer = rng.choices(customers, weights=weights, k=1)[0]
        d = DATA_START + timedelta(days=rng.randint(0, span_days))
        ts = datetime(
            d.year, d.month, d.day, rng.randint(0, 23), rng.randint(0, 59), rng.randint(0, 59)
        )
        status = rng.choice(STATUSES)
        gross = 0.0
        for _ in range(rng.choice([1, 1, 2, 2, 3, 4])):
            p = rng.choice(products)
            qty = rng.choice([1, 1, 1, 2, 2, 3])
            price = _money(p["list_price"] * rng.uniform(0.85, 1.0))
            items.append(
                {
                    "order_item_id": item_id,
                    "order_id": order_id,
                    "product_id": p["product_id"],
                    "quantity": qty,
                    "unit_price": price,
                }
            )
            item_id += 1
            gross += qty * price
        gross = _money(gross)
        discount = _money(gross * rng.uniform(0.05, 0.25)) if rng.random() < 0.4 else 0.0
        shipping = 0.0 if gross > 100 else rng.choice([5.99, 9.99])
        refund = _money(gross * rng.uniform(0.2, 1.0)) if status == "refunded" else 0.0
        orders.append(
            {
                "order_id": order_id,
                "customer_id": customer["customer_id"],
                "order_date": d,
                "order_ts": ts,
                "order_year": d.year,
                "status": status,
                "channel": rng.choice(CHANNELS),
                "ship_country": customer["country"]
                if rng.random() < 0.9
                else rng.choice(COUNTRIES),
                "gross_amount": gross,
                "discount_amount": discount,
                "shipping_fee": shipping,
                "refund_amount": refund,
            }
        )
    return orders, items


def _payment_cards(rng: random.Random, n: int, n_customers: int) -> list[dict[str, Any]]:
    return [
        {
            "card_id": i,
            "customer_id": rng.randint(1, n_customers),
            "card_brand": rng.choice(CARD_BRANDS),
            "card_last4": f"{rng.randint(0, 9999):04d}",
            "card_token": f"tok_{rng.getrandbits(64):016x}",
            "expires_month": rng.randint(1, 12),
            "expires_year": rng.randint(2026, 2031),
        }
        for i in range(1, n + 1)
    ]


# Explicit Arrow schemas: the column types the data dictionary promises.
def _schema(*fields: pa.Field[Any]) -> pa.Schema:
    return pa.schema(list(fields))


SCHEMAS: dict[str, pa.Schema] = {
    "customers": _schema(
        pa.field("customer_id", pa.int64()),
        pa.field("full_name", pa.string()),
        pa.field("email", pa.string()),
        pa.field("phone", pa.string()),
        pa.field("city", pa.string()),
        pa.field("state", pa.string()),
        pa.field("country", pa.string()),
        pa.field("segment", pa.string()),
        pa.field("signup_date", pa.date32()),
        pa.field("marketing_opt_in", pa.bool_()),
    ),
    "products": _schema(
        pa.field("product_id", pa.int64()),
        pa.field("sku", pa.string()),
        pa.field("product_name", pa.string()),
        pa.field("category", pa.string()),
        pa.field("subcategory", pa.string()),
        pa.field("unit_cost", pa.float64()),
        pa.field("list_price", pa.float64()),
        pa.field("is_active", pa.bool_()),
    ),
    "orders": _schema(
        pa.field("order_id", pa.int64()),
        pa.field("customer_id", pa.int64()),
        pa.field("order_date", pa.date32()),
        pa.field("order_ts", pa.timestamp("us")),
        pa.field("order_year", pa.int32()),
        pa.field("status", pa.string()),
        pa.field("channel", pa.string()),
        pa.field("ship_country", pa.string()),
        pa.field("gross_amount", pa.float64()),
        pa.field("discount_amount", pa.float64()),
        pa.field("shipping_fee", pa.float64()),
        pa.field("refund_amount", pa.float64()),
    ),
    "order_items": _schema(
        pa.field("order_item_id", pa.int64()),
        pa.field("order_id", pa.int64()),
        pa.field("product_id", pa.int64()),
        pa.field("quantity", pa.int32()),
        pa.field("unit_price", pa.float64()),
    ),
    "payment_cards": _schema(
        pa.field("card_id", pa.int64()),
        pa.field("customer_id", pa.int64()),
        pa.field("card_brand", pa.string()),
        pa.field("card_last4", pa.string()),
        pa.field("card_token", pa.string()),
        pa.field("expires_month", pa.int32()),
        pa.field("expires_year", pa.int32()),
    ),
}
PARTITIONS: dict[str, list[str]] = {"orders": ["order_year"]}


def generate(spec: SeedSpec | None = None, seed: int = SEED) -> dict[str, pa.Table]:
    """Build all tables in memory. Pure function of (spec, seed)."""
    spec = spec or SeedSpec()
    rng = random.Random(seed)  # noqa: S311 - synthetic data, not security
    customers = _customers(rng, spec.customers)
    products = _products(rng, spec.products)
    orders, items = _orders_and_items(rng, spec.orders, customers, products)
    cards = _payment_cards(rng, spec.payment_cards, spec.customers)
    frames = {
        "customers": customers,
        "products": products,
        "orders": orders,
        "order_items": items,
        "payment_cards": cards,
    }
    return {name: pa.Table.from_pylist(rows, schema=SCHEMAS[name]) for name, rows in frames.items()}


def write(tables: dict[str, pa.Table], out: Path) -> dict[str, int]:
    """Write every table as a Delta table under ``out/<name>`` (idempotent: overwrite)."""
    out.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for name, table in tables.items():
        write_deltalake(
            str(out / name),
            table,
            mode="overwrite",
            partition_by=PARTITIONS.get(name),
        )
        counts[name] = table.num_rows
    manifest = {
        "seed": SEED,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "data_range": [DATA_START.isoformat(), DATA_END.isoformat()],
        "row_counts": counts,
        "format": "delta",
    }
    (out / "MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return counts


def seed_lakehouse(out: Path, scale: float = 1.0) -> dict[str, int]:
    return write(generate(SeedSpec().scaled(scale)), out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed the synthetic retail Delta lakehouse.")
    parser.add_argument("--out", type=Path, default=Path("data/lakehouse"))
    parser.add_argument("--scale", type=float, default=1.0, help="Row-count multiplier.")
    args = parser.parse_args(argv)
    counts = seed_lakehouse(args.out, args.scale)
    total = sum(counts.values())
    for name, n in counts.items():
        print(f"  {name:<14} {n:>6} rows")
    print(f"  {'total':<14} {total:>6} rows -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
