"""reconcile schema with the pre-existing live database (Fase 2)

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-16

0001's CREATE TABLE/CREATE TYPE statements silently failed for everything that already
existed in the user's database from before this project's Alembic setup (an earlier,
partial hand-built schema). Confirmed via the user's own DDL dump + read-only
introspection exactly what's missing, and that EVERY affected table has 0 rows — so
every fix below is applied directly, no backfill needed.

Found and fixed:
  - categories: missing `company_id` (catalog is per-store, decided 2026-09-08);
    `department_id` was nullable, should be required; no duplicate-name protection
    (the TCC's own UC005 expects one).
  - products: missing `filial_id` (catalog is per-store); sku/barcode were UNIQUE
    table-wide instead of per-store.
  - order_items: missing `cost_at_sale` (margin-at-sale snapshot, decided 2026-09-09).
  - orders: missing `payment_method`/`discount` (gap vs. the documented UI prototypes).
  - stock_movements: the whole table was missing (never created).
  - payment_method / stock_movement_reason: enum types were missing entirely.
  - order_status: existed with 7 e-commerce-shaped values (processing/shipped/
    delivered/refunded) left over from an earlier draft — no shipping concept in this
    domain (in-person retail only), shrunk to the 3 actually used. Safe to change the
    type in place (not just add values) because the table is empty.

As always, written by hand and NOT run by Claude — the user applies it themselves.
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


UPGRADE_SQL = """
-- new enum types that never got created
CREATE TYPE payment_method AS ENUM ('cash', 'debit_card', 'credit_card', 'pix', 'other');
CREATE TYPE stock_movement_reason AS ENUM ('sale', 'adjustment', 'loss', 'return', 'restock');

-- order_status: shrink from the old 7-value e-commerce-shaped enum to the 3 actually used
ALTER TABLE orders ALTER COLUMN status DROP DEFAULT;
ALTER TYPE order_status RENAME TO order_status_old;
CREATE TYPE order_status AS ENUM ('pending', 'paid', 'cancelled');
ALTER TABLE orders ALTER COLUMN status TYPE order_status USING status::text::order_status;
ALTER TABLE orders ALTER COLUMN status SET DEFAULT 'pending'::order_status;
DROP TYPE order_status_old;

-- categories: per-store column + required department + duplicate-name guard
ALTER TABLE categories ADD COLUMN company_id bigint REFERENCES companies(id);
ALTER TABLE categories ALTER COLUMN company_id SET NOT NULL;
ALTER TABLE categories ALTER COLUMN department_id SET NOT NULL;
CREATE INDEX ix_categories_company_id ON categories(company_id);
ALTER TABLE categories ADD CONSTRAINT uq_categories_company_name UNIQUE (company_id, name);

-- products: per-store column + fix uniqueness scope
ALTER TABLE products ADD COLUMN filial_id bigint REFERENCES companies(id);
ALTER TABLE products ALTER COLUMN filial_id SET NOT NULL;
CREATE INDEX ix_products_filial_id ON products(filial_id);

ALTER TABLE products DROP CONSTRAINT uq_products_sku;
ALTER TABLE products ADD CONSTRAINT uq_products_sku UNIQUE (filial_id, sku);

ALTER TABLE products DROP CONSTRAINT uq_products_barcode;
ALTER TABLE products ADD CONSTRAINT uq_products_barcode UNIQUE (filial_id, barcode);

-- order_items: margin-at-sale snapshot
ALTER TABLE order_items ADD COLUMN cost_at_sale numeric(12,2);
ALTER TABLE order_items ALTER COLUMN cost_at_sale SET NOT NULL;
ALTER TABLE order_items ADD CONSTRAINT ck_order_items_cost_at_sale CHECK (cost_at_sale >= 0);

-- orders: discount + payment method
ALTER TABLE orders ADD COLUMN discount numeric(12,2) NOT NULL DEFAULT 0;
ALTER TABLE orders ADD CONSTRAINT ck_orders_discount CHECK (discount >= 0);

ALTER TABLE orders ADD COLUMN payment_method payment_method;
UPDATE orders SET payment_method = 'other' WHERE payment_method IS NULL; -- no-op, 0 rows
ALTER TABLE orders ALTER COLUMN payment_method SET NOT NULL;

-- stock_movements: the whole table was missing
CREATE TABLE stock_movements (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    product_id     bigint NOT NULL REFERENCES products(id),
    filial_id      bigint NOT NULL REFERENCES companies(id),
    change_qty     integer NOT NULL CHECK (change_qty <> 0),
    reason         stock_movement_reason NOT NULL,
    resulting_qty  integer NOT NULL CHECK (resulting_qty >= 0),
    order_item_id  bigint REFERENCES order_items(id) ON DELETE SET NULL,
    occurred_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_stock_movements_product_id    ON stock_movements(product_id);
CREATE INDEX ix_stock_movements_filial_id     ON stock_movements(filial_id);
CREATE INDEX ix_stock_movements_order_item_id ON stock_movements(order_item_id);
COMMENT ON TABLE stock_movements IS
    'Fato histórico, não deriva de nada — usado pela camada de Analytics/Forecasting para '
    'distinguir queda de demanda de ruptura de estoque. Nunca fazer UPDATE em stocks.quantity '
    'sem gravar aqui na mesma transação.';
"""

DOWNGRADE_SQL = """
DROP TABLE IF EXISTS stock_movements;

ALTER TABLE orders DROP COLUMN payment_method;
ALTER TABLE orders DROP CONSTRAINT ck_orders_discount;
ALTER TABLE orders DROP COLUMN discount;

ALTER TABLE order_items DROP CONSTRAINT ck_order_items_cost_at_sale;
ALTER TABLE order_items DROP COLUMN cost_at_sale;

ALTER TABLE products DROP CONSTRAINT uq_products_barcode;
ALTER TABLE products ADD CONSTRAINT uq_products_barcode UNIQUE (barcode);

ALTER TABLE products DROP CONSTRAINT uq_products_sku;
ALTER TABLE products ADD CONSTRAINT uq_products_sku UNIQUE (sku);

DROP INDEX IF EXISTS ix_products_filial_id;
ALTER TABLE products DROP COLUMN filial_id;

ALTER TABLE categories DROP CONSTRAINT uq_categories_company_name;
ALTER TABLE categories ALTER COLUMN department_id DROP NOT NULL;
DROP INDEX IF EXISTS ix_categories_company_id;
ALTER TABLE categories DROP COLUMN company_id;

ALTER TABLE orders ALTER COLUMN status DROP DEFAULT;
ALTER TYPE order_status RENAME TO order_status_new;
CREATE TYPE order_status AS ENUM ('pending', 'paid', 'processing', 'shipped', 'delivered', 'cancelled', 'refunded');
ALTER TABLE orders ALTER COLUMN status TYPE order_status USING status::text::order_status;
ALTER TABLE orders ALTER COLUMN status SET DEFAULT 'pending'::order_status;
DROP TYPE order_status_new;

DROP TYPE stock_movement_reason;
DROP TYPE payment_method;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
