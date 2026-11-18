"""companies.plan: subscription plan per store, Pro or Hulk (Fase 12, RF09 / UC012)

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-18

TCC, Figura 16 (planos de monetização): both plans have the hybrid sales app, the
"dashboards inteligentes" and the "análise inteligente" (analytics and forecasts); only Hulk
has the "Assistente IA" — the chat (RF08) and the agent (RF09). What each plan unlocks lives in
app/core/plans.py (no migration to change it).

The plan is per store (decided 2026-11-18): there is no "account" above the stores, and an
owner may have stores on different plans. Only a superuser changes it (no real payment in
the TCC); every change goes to the audit trail.

Existing stores become Hulk, so nothing that works today stops; a new store starts as Pro.

As always, written by hand and NOT run by Claude — applied manually.
"""

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE TYPE subscription_plan AS ENUM ('pro', 'hulk')")
    op.execute("ALTER TABLE companies ADD COLUMN plan subscription_plan NOT NULL DEFAULT 'pro'")
    op.execute("UPDATE companies SET plan = 'hulk'")


def downgrade() -> None:
    op.execute("ALTER TABLE companies DROP COLUMN plan")
    op.execute("DROP TYPE subscription_plan")
