"""categories.status: "delete" a category = deactivate it (RF04)

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-14

A hard DELETE can't work: products (active or not) reference the category, and the mobile
pull is incremental — a deleted row would never leave the app's local table. Same
soft-delete convention as products/clients: DELETE /catalog/categories/{id} sets
status='inactive', refused (409) while an active product still uses it. Existing
categories start as 'active'. Reuses the record_status enum from 0001.

As always, written by hand and NOT run by Claude — applied manually.
"""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE categories ADD COLUMN status record_status NOT NULL DEFAULT 'active'")


def downgrade() -> None:
    op.execute("ALTER TABLE categories DROP COLUMN status")
