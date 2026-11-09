"""order_items.discount_allocated + net_total: one official (net) revenue (audit A4)

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-07

Audit 2026-11-07 (A4): the same period showed two revenues — sales summary / by client /
seller / filial used orders.total (net of the order discount) while sales by category, top
products, margin reports and the whole analytics/forecasting stack used order_items.total
(gross). Margins were overstated by every discount granted.

Team decision: the official revenue is NET. The order-level discount is allocated to its
items in proportion to each item's gross value (rounded to cents; the rounding remainder
goes to the item with the largest gross value, so the shares always add up to exactly the
order discount). Frozen at sale time like unit_price and cost_at_sale.

- discount_allocated: this item's share of orders.discount.
- net_total (generated): quantity * unit_price - discount_allocated. For every order,
  SUM(net_total) = orders.total. order_items.total keeps meaning GROSS (unchanged contract).

Existing orders are backfilled with the same rule. After applying, rebuild analytics
(`python -m app.workers.cli analytics rebuild`) — daily_sales.revenue becomes net.

As always, written by hand and NOT run by Claude — applied manually.
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


UPGRADE_SQL = """
ALTER TABLE order_items ADD COLUMN discount_allocated numeric(12,2) NOT NULL DEFAULT 0;

WITH base AS (
    SELECT i.id, i.order_id, o.discount,
           i.quantity * i.unit_price AS gross,
           sum(i.quantity * i.unit_price) OVER (PARTITION BY i.order_id) AS order_gross,
           row_number() OVER (PARTITION BY i.order_id ORDER BY i.quantity * i.unit_price DESC, i.id) AS rn
    FROM order_items i
    JOIN orders o ON o.id = i.order_id
    WHERE o.discount > 0
),
shares AS (
    SELECT id, order_id, rn, discount, round(discount * gross / nullif(order_gross, 0), 2) AS share
    FROM base
),
final AS (
    SELECT id,
           CASE WHEN rn = 1 THEN share + (discount - sum(share) OVER (PARTITION BY order_id)) ELSE share END AS allocated
    FROM shares
)
UPDATE order_items i SET discount_allocated = final.allocated FROM final WHERE final.id = i.id;

ALTER TABLE order_items ADD CONSTRAINT ck_order_items_discount_allocated
    CHECK (discount_allocated >= 0 AND discount_allocated <= quantity * unit_price);
ALTER TABLE order_items ADD COLUMN net_total numeric(12,2)
    GENERATED ALWAYS AS (quantity * unit_price - discount_allocated) STORED;
"""

DOWNGRADE_SQL = """
ALTER TABLE order_items DROP COLUMN net_total;
ALTER TABLE order_items DROP CONSTRAINT ck_order_items_discount_allocated;
ALTER TABLE order_items DROP COLUMN discount_allocated;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
