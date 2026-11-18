"""second review (2026-11-18): product_costs.effective_at required; redundant FKs and indexes out

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-18

- product_costs.effective_at NOT NULL: it is what keeps a purchase synced late from
  overwriting a newer cost, and a NULL would make that comparison silently pass. The code
  always fills it (manual PUT = now, purchase = when it happened); any old row without it gets
  its updated_at.
- Single-column FKs dropped where a composite FK from 0016 already guarantees the same row
  exists (products(id, filial_id), categories(id, company_id), people(id, filial_id)).
- Indexes dropped where a 0016 composite index starts with the same column.

DROP ... IF EXISTS: this is cleanup — if a database names one of these differently, it is
just left in place. No API change.

As always, written by hand and NOT run by Claude — applied manually.
"""

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

# (table, single-column FK now covered by a composite one)
_REDUNDANT_FKS = [
    ("products", "products_category_id_fkey"),          # fk_products_category_filial
    ("prices", "prices_product_id_fkey"),               # fk_prices_product_filial
    ("stocks", "stocks_product_id_fkey"),               # fk_stocks_product_filial
    ("stock_movements", "stock_movements_product_id_fkey"),  # fk_stock_movements_product_filial
    ("orders", "orders_client_id_fkey"),                # fk_orders_client_filial
    ("purchases", "purchases_supplier_id_fkey"),        # fk_purchases_supplier_filial
]
# index now covered by a composite one starting with the same column
_REDUNDANT_INDEXES = [
    "ix_orders_filial_id",            # ix_orders_filial_effective (filial_id, ...)
    "ix_purchases_filial_id",         # ix_purchases_filial_effective (filial_id, ...)
    "ix_stock_movements_product_id",  # ix_stock_movements_product_filial_date (product_id, ...)
]


def upgrade() -> None:
    op.execute("UPDATE product_costs SET effective_at = updated_at WHERE effective_at IS NULL")
    op.execute("ALTER TABLE product_costs ALTER COLUMN effective_at SET NOT NULL")

    for table, fk in _REDUNDANT_FKS:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {fk}")
    for index in _REDUNDANT_INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {index}")


def downgrade() -> None:
    op.execute("CREATE INDEX IF NOT EXISTS ix_stock_movements_product_id ON stock_movements (product_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_purchases_filial_id ON purchases (filial_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_orders_filial_id ON orders (filial_id)")

    op.execute("ALTER TABLE purchases ADD CONSTRAINT purchases_supplier_id_fkey FOREIGN KEY (supplier_id) REFERENCES people(id)")
    op.execute("ALTER TABLE orders ADD CONSTRAINT orders_client_id_fkey FOREIGN KEY (client_id) REFERENCES people(id)")
    op.execute("ALTER TABLE stock_movements ADD CONSTRAINT stock_movements_product_id_fkey "
               "FOREIGN KEY (product_id) REFERENCES products(id)")
    op.execute("ALTER TABLE stocks ADD CONSTRAINT stocks_product_id_fkey "
               "FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE RESTRICT")
    op.execute("ALTER TABLE prices ADD CONSTRAINT prices_product_id_fkey "
               "FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE RESTRICT")
    op.execute("ALTER TABLE products ADD CONSTRAINT products_category_id_fkey "
               "FOREIGN KEY (category_id) REFERENCES categories(id)")

    op.execute("ALTER TABLE product_costs ALTER COLUMN effective_at DROP NOT NULL")
