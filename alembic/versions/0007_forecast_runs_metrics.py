"""forecast_runs.metrics: holdout accuracy of each run (Fase 8)

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-03

Team decision (2026-11-03): measure accuracy already in Fase 8 — each run is also
backtested by hiding its last 28 training days, forecasting them, and comparing against
what actually sold (MAE, WAPE, bias), at product and filial level. Stored per run so
models can be compared and GET /forecasts can default to the most accurate one.
Fase 9 still owns the real evaluation (forecasts vs. days that hadn't happened yet).

Additive and nullable — existing runs simply have no metrics.

As always, written by hand and NOT run by Claude — applied manually.
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE forecasting.forecast_runs ADD COLUMN metrics jsonb")


def downgrade() -> None:
    op.execute("ALTER TABLE forecasting.forecast_runs DROP COLUMN metrics")
