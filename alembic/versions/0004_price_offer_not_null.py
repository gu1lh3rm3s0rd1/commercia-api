"""prices.price_offer: NULL -> 0, NOT NULL DEFAULT 0 (team convention: price/cost never null)

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-31

Team decision (2026-10-31): price/cost values are never null anywhere in the API — 0
instead. prices.price_offer was the only money column in the schema that still allowed
NULL (cost_value, cost_at_sale, discount, price_default are all NOT NULL already).
0 means "no offer": sales_service only sells at the offer when price_offer > 0.

Existing NULLs are backfilled to 0, then the column becomes NOT NULL DEFAULT 0, and
ck_prices_offer_lower drops its now-impossible "IS NULL OR" branch.

Downgrade restores the nullable column and the original check, but can NOT turn the
backfilled zeros back into NULL — there is no record of which rows were NULL. Restore
from a backup if the exact previous state is needed.

As always, written by hand and NOT run by Claude — applied manually.
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


UPGRADE_SQL = """
UPDATE prices SET price_offer = 0 WHERE price_offer IS NULL;
ALTER TABLE prices ALTER COLUMN price_offer SET DEFAULT 0;
ALTER TABLE prices ALTER COLUMN price_offer SET NOT NULL;

ALTER TABLE prices DROP CONSTRAINT ck_prices_offer_lower;
ALTER TABLE prices ADD CONSTRAINT ck_prices_offer_lower CHECK (price_offer <= price_default);
"""

DOWNGRADE_SQL = """
ALTER TABLE prices DROP CONSTRAINT ck_prices_offer_lower;
ALTER TABLE prices ADD CONSTRAINT ck_prices_offer_lower
    CHECK (price_offer IS NULL OR price_offer <= price_default);

ALTER TABLE prices ALTER COLUMN price_offer DROP NOT NULL;
ALTER TABLE prices ALTER COLUMN price_offer DROP DEFAULT;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
