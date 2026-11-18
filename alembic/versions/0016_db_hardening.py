"""database hardening after the external review (2026-11-18): the DB now guarantees what the
services already enforced — no API route or contract changes

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-18

1. History can't be deleted: prices, stocks and product_costs no longer CASCADE from products,
   and stock movements no longer go SET NULL when an order/purchase item is deleted — all
   RESTRICT. (The API never deletes these: products are deactivated, orders/purchases
   cancelled. This closes the door to a manual DELETE corrupting the ledger.)
2. Stores can't be mixed: composite foreign keys tie stocks, prices, stock movements and the
   product's category to the product's own store, and an order's client / a purchase's
   supplier to the order's/purchase's store.
3. No overlapping price periods per product (exclusion constraint, btree_gist). Periods are
   HALF-OPEN [start_date, end_date): the API closes the previous price with end_date = the new
   price's start, and on that day the new one applies (get_price_at takes the latest start).
   A price closed on the day it started (re-priced twice the same day, a same-day correction)
   is an empty period and conflicts with nothing.
4. stock_movements: created_by_id (who moved the stock — backfilled from the order, the
   purchase or the audit log) and the reason tied to its source: sale ⇒ an order item,
   purchase ⇒ a purchase item, loss/return/restock ⇒ manual (none), adjustment ⇒ manual or
   the reversal of a cancelled sale/purchase.
5. The same NF-e can't be registered twice in a store (cancelled purchases excepted, so a
   purchase can be cancelled and registered again).
6. users: e-mail and username unique regardless of case (the service already compared them
   case-insensitively; now the DB does too).
7. Report indexes on when the sale/purchase really happened, and for a product's stock
   history.

Every rule is checked against the existing data first: if something doesn't comply, the
migration stops with a message saying what (nothing is fixed silently).

As always, written by hand and NOT run by Claude — applied manually.
"""

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

# half-open period [start_date, end_date): a price closed on the day it started is empty, so
# a same-day correction (two prices the same day — the latest wins) is still allowed
_PERIOD = "daterange(start_date, end_date, '[)')"


def _require_none(sql: str, message: str) -> None:
    """Stops the migration with `message` when `sql` (a SELECT count(*)) is above zero."""
    op.execute(f"""
        DO $$
        DECLARE n bigint;
        BEGIN
            {sql.strip().rstrip(';')} INTO n;
            IF n > 0 THEN
                RAISE EXCEPTION '0016: % — %', n, '{message}';
            END IF;
        END $$
    """)


def upgrade() -> None:
    # ---------------- pre-checks on the existing data ----------------
    _require_none("SELECT count(*) FROM stocks s JOIN products p ON p.id = s.product_id WHERE p.filial_id <> s.filial_id",
                  "stocks with a filial different from the product''s")
    _require_none("SELECT count(*) FROM prices s JOIN products p ON p.id = s.product_id WHERE p.filial_id <> s.filial_id",
                  "prices with a filial different from the product''s")
    _require_none("SELECT count(*) FROM stock_movements s JOIN products p ON p.id = s.product_id WHERE p.filial_id <> s.filial_id",
                  "stock movements with a filial different from the product''s")
    _require_none("SELECT count(*) FROM products p JOIN categories c ON c.id = p.category_id WHERE c.company_id <> p.filial_id",
                  "products whose category belongs to another filial")
    _require_none("SELECT count(*) FROM orders o JOIN people x ON x.id = o.client_id WHERE x.filial_id <> o.filial_id",
                  "orders whose client belongs to another filial")
    _require_none("SELECT count(*) FROM purchases o JOIN people x ON x.id = o.supplier_id WHERE x.filial_id <> o.filial_id",
                  "purchases whose supplier belongs to another filial")
    _require_none(f"""SELECT count(*) FROM prices a JOIN prices b ON a.product_id = b.product_id AND a.filial_id = b.filial_id
                      AND a.id < b.id AND {_PERIOD.replace('start_date', 'a.start_date').replace('end_date', 'a.end_date')}
                      && {_PERIOD.replace('start_date', 'b.start_date').replace('end_date', 'b.end_date')}""",
                  "pairs of prices with overlapping periods")
    _require_none("""SELECT count(*) FROM stock_movements WHERE NOT (
                        (reason = 'sale' AND order_item_id IS NOT NULL AND purchase_item_id IS NULL)
                        OR (reason = 'purchase' AND purchase_item_id IS NOT NULL AND order_item_id IS NULL)
                        OR (reason IN ('loss', 'return', 'restock') AND order_item_id IS NULL AND purchase_item_id IS NULL)
                        OR reason = 'adjustment')""",
                  "stock movements whose reason doesn''t match their source")
    _require_none("""SELECT count(*) FROM (SELECT 1 FROM purchases WHERE invoice_key IS NOT NULL AND status <> 'cancelled'
                     GROUP BY filial_id, invoice_key HAVING count(*) > 1) d""",
                  "NF-e keys registered twice in the same filial")
    _require_none("SELECT count(*) - count(DISTINCT lower(email)) FROM users", "e-mails repeated ignoring case")
    _require_none("SELECT count(*) - count(DISTINCT lower(username)) FROM users", "usernames repeated ignoring case")

    # ---------------- 1. history can't be deleted ----------------
    for table in ("prices", "stocks", "product_costs"):
        op.execute(f"""
            ALTER TABLE {table} DROP CONSTRAINT {table}_product_id_fkey,
                ADD CONSTRAINT {table}_product_id_fkey FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE RESTRICT
        """)
    op.execute("""
        ALTER TABLE stock_movements
            DROP CONSTRAINT stock_movements_order_item_id_fkey,
            ADD CONSTRAINT stock_movements_order_item_id_fkey
                FOREIGN KEY (order_item_id) REFERENCES order_items(id) ON DELETE RESTRICT,
            DROP CONSTRAINT stock_movements_purchase_item_id_fkey,
            ADD CONSTRAINT stock_movements_purchase_item_id_fkey
                FOREIGN KEY (purchase_item_id) REFERENCES purchase_items(id) ON DELETE RESTRICT
    """)

    # ---------------- 2. stores can't be mixed ----------------
    op.execute("ALTER TABLE products ADD CONSTRAINT uq_products_id_filial UNIQUE (id, filial_id)")
    op.execute("ALTER TABLE categories ADD CONSTRAINT uq_categories_id_company UNIQUE (id, company_id)")
    op.execute("ALTER TABLE people ADD CONSTRAINT uq_people_id_filial UNIQUE (id, filial_id)")
    for table in ("stocks", "prices", "stock_movements"):
        op.execute(f"""
            ALTER TABLE {table} ADD CONSTRAINT fk_{table}_product_filial
                FOREIGN KEY (product_id, filial_id) REFERENCES products(id, filial_id) ON DELETE RESTRICT
        """)
    op.execute("""
        ALTER TABLE products ADD CONSTRAINT fk_products_category_filial
            FOREIGN KEY (category_id, filial_id) REFERENCES categories(id, company_id)
    """)
    op.execute("""
        ALTER TABLE orders ADD CONSTRAINT fk_orders_client_filial
            FOREIGN KEY (client_id, filial_id) REFERENCES people(id, filial_id)
    """)
    op.execute("""
        ALTER TABLE purchases ADD CONSTRAINT fk_purchases_supplier_filial
            FOREIGN KEY (supplier_id, filial_id) REFERENCES people(id, filial_id)
    """)

    # ---------------- 3. no overlapping price periods ----------------
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    op.execute(f"""
        ALTER TABLE prices ADD CONSTRAINT ex_prices_no_overlap
            EXCLUDE USING gist (product_id WITH =, filial_id WITH =, ({_PERIOD}) WITH &&)
    """)

    # ---------------- 4. stock movements: who, and reason tied to source ----------------
    op.execute("ALTER TABLE stock_movements ADD COLUMN created_by_id bigint REFERENCES users(id)")
    op.execute("CREATE INDEX ix_stock_movements_created_by_id ON stock_movements (created_by_id)")
    # backfill: sales and their cancellations from the order, purchases from who received it,
    # manual adjustments from the audit trail (rows from before the trail existed stay NULL)
    op.execute("""
        UPDATE stock_movements m SET created_by_id = o.created_by_id
        FROM order_items i JOIN orders o ON o.id = i.order_id
        WHERE m.order_item_id = i.id AND m.reason = 'sale'
    """)
    op.execute("""
        UPDATE stock_movements m SET created_by_id = p.received_by_id
        FROM purchase_items i JOIN purchases p ON p.id = i.purchase_id
        WHERE m.purchase_item_id = i.id AND m.reason = 'purchase'
    """)
    op.execute("""
        UPDATE stock_movements m SET created_by_id = a.user_id
        FROM audit_logs a
        WHERE a.entity_type = 'stock' AND a.action = 'stock.adjust' AND a.entity_id = m.id AND m.created_by_id IS NULL
    """)
    op.execute("""
        ALTER TABLE stock_movements ADD CONSTRAINT ck_stock_movements_reason_source CHECK (
            (reason = 'sale' AND order_item_id IS NOT NULL AND purchase_item_id IS NULL)
            OR (reason = 'purchase' AND purchase_item_id IS NOT NULL AND order_item_id IS NULL)
            OR (reason IN ('loss', 'return', 'restock') AND order_item_id IS NULL AND purchase_item_id IS NULL)
            OR reason = 'adjustment'
        )
    """)

    # ---------------- 5. NF-e once per store ----------------
    op.execute("""
        CREATE UNIQUE INDEX uq_purchases_filial_invoice_key ON purchases (filial_id, invoice_key)
        WHERE invoice_key IS NOT NULL AND status <> 'cancelled'
    """)

    # ---------------- 6. e-mail / username regardless of case ----------------
    op.execute("ALTER TABLE users DROP CONSTRAINT uq_users_email")
    op.execute("ALTER TABLE users DROP CONSTRAINT uq_users_username")
    op.execute("CREATE UNIQUE INDEX uq_users_email_lower ON users (lower(email))")
    op.execute("CREATE UNIQUE INDEX uq_users_username_lower ON users (lower(username))")

    # ---------------- 7. report indexes ----------------
    op.execute("CREATE INDEX ix_orders_filial_effective ON orders (filial_id, (COALESCE(occurred_at, created_at)))")
    op.execute("CREATE INDEX ix_purchases_filial_effective ON purchases (filial_id, (COALESCE(occurred_at, created_at)))")
    op.execute("CREATE INDEX ix_stock_movements_product_filial_date ON stock_movements (product_id, filial_id, occurred_at)")


def downgrade() -> None:
    op.execute("DROP INDEX ix_stock_movements_product_filial_date")
    op.execute("DROP INDEX ix_purchases_filial_effective")
    op.execute("DROP INDEX ix_orders_filial_effective")

    op.execute("DROP INDEX uq_users_username_lower")
    op.execute("DROP INDEX uq_users_email_lower")
    op.execute("ALTER TABLE users ADD CONSTRAINT uq_users_username UNIQUE (username)")
    op.execute("ALTER TABLE users ADD CONSTRAINT uq_users_email UNIQUE (email)")

    op.execute("DROP INDEX uq_purchases_filial_invoice_key")

    op.execute("ALTER TABLE stock_movements DROP CONSTRAINT ck_stock_movements_reason_source")
    op.execute("DROP INDEX ix_stock_movements_created_by_id")
    op.execute("ALTER TABLE stock_movements DROP COLUMN created_by_id")

    op.execute("ALTER TABLE prices DROP CONSTRAINT ex_prices_no_overlap")  # btree_gist stays installed

    op.execute("ALTER TABLE purchases DROP CONSTRAINT fk_purchases_supplier_filial")
    op.execute("ALTER TABLE orders DROP CONSTRAINT fk_orders_client_filial")
    op.execute("ALTER TABLE products DROP CONSTRAINT fk_products_category_filial")
    for table in ("stocks", "prices", "stock_movements"):
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT fk_{table}_product_filial")
    op.execute("ALTER TABLE people DROP CONSTRAINT uq_people_id_filial")
    op.execute("ALTER TABLE categories DROP CONSTRAINT uq_categories_id_company")
    op.execute("ALTER TABLE products DROP CONSTRAINT uq_products_id_filial")

    op.execute("""
        ALTER TABLE stock_movements
            DROP CONSTRAINT stock_movements_order_item_id_fkey,
            ADD CONSTRAINT stock_movements_order_item_id_fkey
                FOREIGN KEY (order_item_id) REFERENCES order_items(id) ON DELETE SET NULL,
            DROP CONSTRAINT stock_movements_purchase_item_id_fkey,
            ADD CONSTRAINT stock_movements_purchase_item_id_fkey
                FOREIGN KEY (purchase_item_id) REFERENCES purchase_items(id) ON DELETE SET NULL
    """)
    for table in ("prices", "stocks", "product_costs"):
        op.execute(f"""
            ALTER TABLE {table} DROP CONSTRAINT {table}_product_id_fkey,
                ADD CONSTRAINT {table}_product_id_fkey FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
        """)
