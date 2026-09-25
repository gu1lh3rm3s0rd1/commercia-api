"""mobile sync: idempotency key + occurred_at on orders, updated_at on catalog (Fase 5)

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-25

Adds what RF10/RF11 (mobile sync) needs:
  - orders.client_reference: UUID set by the mobile app at the moment of an offline
    sale, unique — the idempotency key that lets a resent batch not create duplicate
    sales. Nullable: online orders (Fase 4's regular POST /orders) never set it.
  - orders.occurred_at: when the sale actually happened offline, as opposed to
    created_at (when the row was written/synced). Nullable: for online orders,
    created_at already IS the occurrence time.
  - categories.updated_at / products.updated_at: were missing entirely — an edit via
    the existing Fase 2 PATCH endpoints was invisible to an incremental sync pull
    based on created_at alone. Backfilled to now() for any existing rows, same
    pattern as orders/stocks.

Both databases (local dev and the live Neon deploy) already have real data at this
point, but every change below is purely additive (new nullable columns, or NOT NULL
with a DEFAULT that Postgres backfills) — safe to apply directly, no data migration
needed. As always, written by hand and NOT run by Claude — applied manually.
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


UPGRADE_SQL = """
ALTER TABLE orders ADD COLUMN client_reference uuid;
ALTER TABLE orders ADD CONSTRAINT uq_orders_client_reference UNIQUE (client_reference);
ALTER TABLE orders ADD COLUMN occurred_at timestamptz;

ALTER TABLE categories ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now();
CREATE INDEX ix_categories_updated_at ON categories(updated_at);

ALTER TABLE products ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now();
CREATE INDEX ix_products_updated_at ON products(updated_at);

CREATE INDEX ix_prices_created_at ON prices(created_at);
CREATE INDEX ix_stocks_updated_at ON stocks(updated_at);
"""

DOWNGRADE_SQL = """
DROP INDEX IF EXISTS ix_stocks_updated_at;
DROP INDEX IF EXISTS ix_prices_created_at;

DROP INDEX IF EXISTS ix_products_updated_at;
ALTER TABLE products DROP COLUMN updated_at;

DROP INDEX IF EXISTS ix_categories_updated_at;
ALTER TABLE categories DROP COLUMN updated_at;

ALTER TABLE orders DROP COLUMN occurred_at;
ALTER TABLE orders DROP CONSTRAINT uq_orders_client_reference;
ALTER TABLE orders DROP COLUMN client_reference;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
