"""forecast evaluation: forecast_runs.kind + forecast_evaluations (Fase 9)

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-06

- forecast_runs.kind: 'forecast' (a real forecast of the future, what the API serves) or
  'backtest' (a retroactive run: trained as if "today" were a past date, so the days it
  predicts already happened and can be scored right away). Without it the API — which
  serves the newest run — would start serving a backtest made after the real forecast but
  looking at the past. Existing runs are all real forecasts, hence the default.
- forecast_evaluations: one row per run x level x horizon bucket ('1-7', '8-14', '15+'
  days ahead), comparing forecasts.predicted with what really sold (analytics.daily_sales)
  on days that already happened. Derived and fully recomputed by `forecast evaluate`;
  run_id cascades, so a deleted run takes its evaluations with it.
  WAPE (total |error| / total sold) is the headline metric; MAPE is only computed at
  aggregated levels on days with sales — at product level most days sell 0 or 1, where
  a per-day percentage is undefined or explodes.

As always, written by hand and NOT run by Claude — applied manually.
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


UPGRADE_SQL = """
ALTER TABLE forecasting.forecast_runs ADD COLUMN kind varchar(20) NOT NULL DEFAULT 'forecast';
ALTER TABLE forecasting.forecast_runs
    ADD CONSTRAINT ck_forecast_runs_kind CHECK (kind IN ('forecast', 'backtest'));

CREATE TABLE forecasting.forecast_evaluations (
    run_id           bigint        NOT NULL REFERENCES forecasting.forecast_runs(id) ON DELETE CASCADE,
    level            varchar(20)   NOT NULL,
    horizon_bucket   varchar(10)   NOT NULL,
    n_points         int           NOT NULL,
    actual_total     numeric(16,2) NOT NULL,
    abs_error_total  numeric(16,2) NOT NULL,
    mae              numeric(14,4) NOT NULL,
    wape             numeric(10,4),
    mape             numeric(10,4),
    bias             numeric(10,4),
    evaluated_at     timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, level, horizon_bucket),
    CONSTRAINT ck_forecast_evaluations_level CHECK (level IN ('product', 'category', 'department', 'filial')),
    CONSTRAINT ck_forecast_evaluations_bucket CHECK (horizon_bucket IN ('1-7', '8-14', '15+'))
);
"""

DOWNGRADE_SQL = """
DROP TABLE forecasting.forecast_evaluations;
ALTER TABLE forecasting.forecast_runs DROP CONSTRAINT ck_forecast_runs_kind;
ALTER TABLE forecasting.forecast_runs DROP COLUMN kind;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
