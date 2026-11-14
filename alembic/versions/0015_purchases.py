"""purchases + purchase_items (Etapa 3): buying from suppliers, stock in, last cost

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-14

Decided 2026-11-14:
- a purchase comes from a person with the "supplier" role; the cost per unit is typed (from
  the supplier's invoice), unlike a sale's price;
- stock enters when the purchase is RECEIVED (panel: pending → received, or create already
  received; the app's offline purchases always arrive received). Cancelling a received one
  takes the stock back out (refused if it was already sold);
- discount and freight are split across the items in proportion to their value (same rule
  as the sale discount); landed unit cost = (qty * unit_cost - discount share + freight
  share) / qty;
- the product's cost becomes the LAST purchase's landed cost — "last" by when the purchase
  happened (occurred_at offline, received_at online), so an old offline purchase synced late
  doesn't overwrite a newer cost. product_costs.effective_at records when the current cost
  is from (a manual PUT /cost stamps now);
- offline purchases reuse the sales sync model: client_reference (idempotency) + occurred_at.

Adds stock_movement_reason 'purchase' (Postgres 12+ allows ADD VALUE in a transaction as
long as the value isn't used in it — it isn't here). As always, written by hand and NOT run
by Claude — applied manually.
"""

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE stock_movement_reason ADD VALUE IF NOT EXISTS 'purchase'")
    op.execute("CREATE TYPE purchase_status AS ENUM ('pending', 'received', 'cancelled')")

    op.execute("""
        CREATE TABLE purchases (
            id               bigserial PRIMARY KEY,
            supplier_id      bigint NOT NULL REFERENCES people(id),
            filial_id        bigint NOT NULL REFERENCES companies(id),
            status           purchase_status NOT NULL DEFAULT 'pending',
            total            numeric(12,2) NOT NULL DEFAULT 0,
            discount         numeric(12,2) NOT NULL DEFAULT 0,
            freight          numeric(12,2) NOT NULL DEFAULT 0,
            invoice_number   varchar(20),
            invoice_key      varchar(44),
            issued_at        date,
            notes            text,
            client_reference uuid,
            occurred_at      timestamptz,
            received_at      timestamptz,
            created_by_id    bigint REFERENCES users(id),
            received_by_id   bigint REFERENCES users(id),
            created_at       timestamptz NOT NULL DEFAULT now(),
            updated_at       timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_purchases_client_reference UNIQUE (client_reference),
            CONSTRAINT ck_purchases_total CHECK (total >= 0),
            CONSTRAINT ck_purchases_discount CHECK (discount >= 0),
            CONSTRAINT ck_purchases_freight CHECK (freight >= 0),
            CONSTRAINT ck_purchases_invoice_key CHECK (invoice_key ~ '^[0-9]{44}$'),
            -- received ⇒ has received_at; pending ⇒ hasn't (a cancelled one keeps it if it had been received)
            CONSTRAINT ck_purchases_received CHECK ((status <> 'received' OR received_at IS NOT NULL)
                                                    AND (status <> 'pending' OR received_at IS NULL))
        )
    """)
    op.execute("CREATE INDEX ix_purchases_filial_id ON purchases (filial_id)")
    op.execute("CREATE INDEX ix_purchases_supplier_id ON purchases (supplier_id)")
    op.execute("CREATE INDEX ix_purchases_status ON purchases (status)")

    op.execute("""
        CREATE TABLE purchase_items (
            id                bigserial PRIMARY KEY,
            purchase_id       bigint NOT NULL REFERENCES purchases(id) ON DELETE CASCADE,
            product_id        bigint NOT NULL REFERENCES products(id),
            quantity          integer NOT NULL,
            unit_cost         numeric(12,2) NOT NULL,
            total             numeric(12,2) GENERATED ALWAYS AS (quantity * unit_cost) STORED,
            discount_allocated numeric(12,2) NOT NULL DEFAULT 0,
            freight_allocated  numeric(12,2) NOT NULL DEFAULT 0,
            net_total         numeric(12,2) GENERATED ALWAYS AS
                                  (quantity * unit_cost - discount_allocated + freight_allocated) STORED,
            landed_unit_cost  numeric(12,4) GENERATED ALWAYS AS
                                  ((quantity * unit_cost - discount_allocated + freight_allocated) / quantity) STORED,
            created_at        timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_purchase_items_quantity CHECK (quantity > 0),
            CONSTRAINT ck_purchase_items_unit_cost CHECK (unit_cost >= 0),
            CONSTRAINT ck_purchase_items_discount CHECK (discount_allocated >= 0 AND discount_allocated <= quantity * unit_cost),
            CONSTRAINT ck_purchase_items_freight CHECK (freight_allocated >= 0)
        )
    """)
    op.execute("CREATE INDEX ix_purchase_items_purchase_id ON purchase_items (purchase_id)")
    op.execute("CREATE INDEX ix_purchase_items_product_id ON purchase_items (product_id)")

    op.execute("""
        ALTER TABLE stock_movements
            ADD COLUMN purchase_item_id bigint REFERENCES purchase_items(id) ON DELETE SET NULL,
            ADD CONSTRAINT ck_stock_movements_one_source CHECK (order_item_id IS NULL OR purchase_item_id IS NULL)
    """)
    op.execute("CREATE INDEX ix_stock_movements_purchase_item_id ON stock_movements (purchase_item_id)")

    op.execute("ALTER TABLE product_costs ADD COLUMN effective_at timestamptz")


def downgrade() -> None:
    op.execute("ALTER TABLE product_costs DROP COLUMN effective_at")
    op.execute("DROP INDEX ix_stock_movements_purchase_item_id")
    op.execute("ALTER TABLE stock_movements DROP CONSTRAINT ck_stock_movements_one_source")
    op.execute("ALTER TABLE stock_movements DROP COLUMN purchase_item_id")
    op.execute("DROP TABLE purchase_items")
    op.execute("DROP TABLE purchases")
    op.execute("DROP TYPE purchase_status")
    # Postgres can't drop an enum value: 'purchase' stays in stock_movement_reason. Movements
    # already written with it keep it (they were real stock entries).
