# Data Dictionary — Retail Lakehouse

This document defines every table and column the analyst agent may query, plus the
business definitions analysts must use. It is indexed for retrieval; each heading is a
citable section.

Storage: Delta Lake tables under `data/lakehouse/<table>/`, queried through DuckDB views of
the same name. All monetary values are in USD and rounded to cents. All dates are calendar
dates with no time zone; timestamps are UTC.

## Table: customers

One row per registered customer. Contains personal data; see `policies.md`.

| Column | Type | Description |
|---|---|---|
| customer_id | BIGINT | Primary key. |
| full_name | VARCHAR | Customer's full legal name. **PII.** |
| email | VARCHAR | Primary contact email. **PII.** |
| phone | VARCHAR | Primary contact phone in E.164-like format. **PII.** |
| city | VARCHAR | Billing city. |
| state | VARCHAR | Billing state or province code; NULL outside US/CA. |
| country | VARCHAR | ISO 3166-1 alpha-2 billing country (US, CA, GB, DE, FR, ES, MX, BR). |
| segment | VARCHAR | Commercial segment: `consumer`, `smb`, or `enterprise`. |
| signup_date | DATE | Date the account was created. |
| marketing_opt_in | BOOLEAN | Whether the customer consented to marketing contact. |

## Table: products

One row per sellable product. Prices are current list prices; use `order_items.unit_price`
for the price actually charged on an order.

| Column | Type | Description |
|---|---|---|
| product_id | BIGINT | Primary key. |
| sku | VARCHAR | Stock-keeping unit, unique. |
| product_name | VARCHAR | Display name. |
| category | VARCHAR | Top-level category (Electronics, Home, Apparel, Sports, Toys, Grocery, Beauty, Office). |
| subcategory | VARCHAR | Second-level category. |
| unit_cost | DOUBLE | Current cost of goods per unit, USD. |
| list_price | DOUBLE | Current list price per unit, USD. |
| is_active | BOOLEAN | FALSE for discontinued products that may still appear in historical orders. |

## Table: orders

One row per order header. Line items live in `order_items`. Partitioned by `order_year`.

| Column | Type | Description |
|---|---|---|
| order_id | BIGINT | Primary key. |
| customer_id | BIGINT | Foreign key to `customers.customer_id`. |
| order_date | DATE | Calendar date the order was placed. |
| order_ts | TIMESTAMP | UTC timestamp the order was placed. |
| order_year | INTEGER | Partition column, equal to `year(order_date)`. |
| status | VARCHAR | Lifecycle state: `placed`, `shipped`, `delivered`, `cancelled`, `refunded`. |
| channel | VARCHAR | Sales channel: `web`, `mobile`, `store`. |
| ship_country | VARCHAR | ISO 3166-1 alpha-2 shipping country. |
| gross_amount | DOUBLE | Sum of `quantity * unit_price` over the order's items, before discounts. |
| discount_amount | DOUBLE | Total discount applied to the order. |
| shipping_fee | DOUBLE | Shipping charged to the customer. Not revenue. |
| refund_amount | DOUBLE | Amount refunded to the customer; non-zero only when `status = 'refunded'`. |

## Table: order_items

One row per product line on an order.

| Column | Type | Description |
|---|---|---|
| order_item_id | BIGINT | Primary key. |
| order_id | BIGINT | Foreign key to `orders.order_id`. |
| product_id | BIGINT | Foreign key to `products.product_id`. |
| quantity | INTEGER | Units purchased, at least 1. |
| unit_price | DOUBLE | Price per unit actually charged, USD. May differ from `products.list_price`. |

## Table: payment_cards

**Restricted table.** Tokenized payment instruments. The analyst agent must never read it;
see `policies.md`. Documented here only so the restriction is understood.

| Column | Type | Description |
|---|---|---|
| card_id | BIGINT | Primary key. |
| customer_id | BIGINT | Foreign key to `customers.customer_id`. |
| card_brand | VARCHAR | Network brand. |
| card_last4 | VARCHAR | Last four digits. |
| card_token | VARCHAR | Payment-provider token. |
| expires_month | INTEGER | Expiry month. |
| expires_year | INTEGER | Expiry year. |

## Business definitions

### Gross revenue

`gross_revenue = SUM(orders.gross_amount)` over orders whose `status <> 'cancelled'`.
Shipping fees are never revenue.

### Net revenue

`net_revenue = SUM(orders.gross_amount - orders.discount_amount - orders.refund_amount)`
over orders whose `status <> 'cancelled'`. Net revenue **excludes refunds** and discounts,
and excludes shipping fees. This is the default meaning of "revenue" when a question does not
say gross or net.

### Cancelled orders

Orders with `status = 'cancelled'` were never fulfilled. They are excluded from every
revenue, order-count, and average-order-value metric unless the question asks about
cancellations explicitly.

### Average order value (AOV)

`AOV = net_revenue / COUNT(DISTINCT order_id)` over non-cancelled orders in the period.

### Active customer

A customer is active in a period if they have at least one non-cancelled order with
`order_date` in that period.

### Units sold

`units_sold = SUM(order_items.quantity)` joined to non-cancelled orders.

### Product margin

`margin = SUM(order_items.quantity * (order_items.unit_price - products.unit_cost))` over
non-cancelled orders. Uses the price charged, not the list price.

### Time periods

Fiscal year equals calendar year. The data covers 2024-01-01 through 2025-12-31. Relative
phrases resolve against the data, not the wall clock: "last year" and "the most recent year"
mean the latest complete calendar year in the data (2025); "the year before" means 2024. The
agent should always state the exact date range it used. Month boundaries use `order_date`, not
`order_ts`.

### Country

"Customer country" means `customers.country` (billing). "Shipping country" means
`orders.ship_country`. When a question says only "country", use the customer's billing
country.
