"""forecasting schema: forecast_runs, forecasts (Fase 8)

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-03

A separate `forecasting` schema — deliberately NOT inside `analytics`. `analytics` is
derived and can be dropped/rebuilt at any time; a forecast can't: it records what a model
predicted *at the time it ran*, which is exactly what Fase 9 compares against what really
happened (forecasts.target_date x analytics.daily_sales). Rebuilding it would erase the
evidence, so it lives where no rebuild ever touches it.

- forecast_runs: one row per generation of one model for one target (quantity | revenue)
  in one filial — which model, which code version, which params, which training window.
  Every forecast row points to one, so no forecast exists without a trace of how it was
  produced (Fase 8's completion criterion).
- forecasts: one row per run x level (product | category | department | filial) x entity
  x target day, with an 80% band (lower/upper). entity_id points to products / categories
  / departments / companies depending on level, hence no FK on it; run_id cascades, so
  deleting a run removes its rows and never leaves orphans.

As always, written by hand and NOT run by Claude — applied manually.
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


UPGRADE_SQL = """
CREATE SCHEMA forecasting;

CREATE TABLE forecasting.forecast_runs (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    model          varchar(40) NOT NULL,
    model_version  varchar(20) NOT NULL,
    params         jsonb       NOT NULL DEFAULT '{}'::jsonb,
    target         varchar(20) NOT NULL,
    filial_id      bigint      NOT NULL,
    train_start    date        NOT NULL,
    train_end      date        NOT NULL,
    horizon_days   int         NOT NULL,
    status         varchar(20) NOT NULL,
    n_series       int,
    created_at     timestamptz NOT NULL DEFAULT now(),
    finished_at    timestamptz,
    error          text,
    CONSTRAINT ck_forecast_runs_target CHECK (target IN ('quantity', 'revenue')),
    CONSTRAINT ck_forecast_runs_status CHECK (status IN ('running', 'success', 'failed')),
    CONSTRAINT ck_forecast_runs_horizon CHECK (horizon_days > 0),
    CONSTRAINT ck_forecast_runs_train_window CHECK (train_end >= train_start)
);
CREATE INDEX ix_forecast_runs_lookup ON forecasting.forecast_runs (filial_id, target, model, created_at DESC);

CREATE TABLE forecasting.forecasts (
    run_id       bigint        NOT NULL REFERENCES forecasting.forecast_runs(id) ON DELETE CASCADE,
    level        varchar(20)   NOT NULL,
    entity_id    bigint        NOT NULL,
    target_date  date          NOT NULL,
    predicted    numeric(14,2) NOT NULL,
    lower        numeric(14,2) NOT NULL,
    upper        numeric(14,2) NOT NULL,
    PRIMARY KEY (run_id, level, entity_id, target_date),
    CONSTRAINT ck_forecasts_level CHECK (level IN ('product', 'category', 'department', 'filial')),
    CONSTRAINT ck_forecasts_band CHECK (lower >= 0 AND lower <= predicted AND predicted <= upper)
);
CREATE INDEX ix_forecasts_entity_date ON forecasting.forecasts (level, entity_id, target_date);
"""

DOWNGRADE_SQL = """
DROP SCHEMA forecasting CASCADE;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
