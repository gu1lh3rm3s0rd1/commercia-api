"""analytics schema: daily_sales, daily_stock_snapshot, job_runs (Fase 7)

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-03

A separate `analytics` schema of data DERIVED from `public` — never a second source of
truth. Everything in it can be dropped and rebuilt at any time by the jobs in
app/analytics/jobs.py (`python -m app.workers.cli analytics rebuild`). That's why there
are deliberately no foreign keys into `public`: a derived table must never block a
normal operation (or a TRUNCATE ... CASCADE) on the operational data.

- daily_sales: grain day x filial x product, SPARSE (a row only when the product sold that
  day). Days are business-timezone days (settings.app_timezone, America/Sao_Paulo).
  Cancelled orders excluded; paid and pending included (same rule as the dashboard).
  revenue = SUM(order_items.total), i.e. before the order-level discount.
- daily_stock_snapshot: grain day x filial x product, DENSE (every day from the product's
  first stock movement up to yesterday). end_of_day_qty carries forward on days without
  movements; was_out_of_stock = the stock reached 0 during that day (a movement left it at
  0), or the whole day passed at 0. This is what lets Fase 8 tell "sold 0 because nobody
  wanted it" from "sold 0 because there was nothing to sell".
- job_runs: one row per job execution. last_order_id/last_stock_movement_id are the
  watermarks the incremental refresh uses to find what changed (stock_movements has no
  "recorded at" column, so ids are the reliable signal).

As always, written by hand and NOT run by Claude — applied manually.
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


UPGRADE_SQL = """
CREATE SCHEMA analytics;

CREATE TABLE analytics.daily_sales (
    sale_date     date          NOT NULL,
    filial_id     bigint        NOT NULL,
    product_id    bigint        NOT NULL,
    orders_count  int           NOT NULL,
    quantity      int           NOT NULL,
    revenue       numeric(14,2) NOT NULL,
    cost          numeric(14,2) NOT NULL,
    PRIMARY KEY (sale_date, filial_id, product_id)
);
CREATE INDEX ix_daily_sales_product_date ON analytics.daily_sales (product_id, sale_date);

CREATE TABLE analytics.daily_stock_snapshot (
    snapshot_date     date    NOT NULL,
    filial_id         bigint  NOT NULL,
    product_id        bigint  NOT NULL,
    end_of_day_qty    int     NOT NULL,
    min_qty           int     NOT NULL,
    was_out_of_stock  boolean NOT NULL,
    PRIMARY KEY (snapshot_date, filial_id, product_id)
);
CREATE INDEX ix_daily_stock_snapshot_product_date ON analytics.daily_stock_snapshot (product_id, snapshot_date);

CREATE TABLE analytics.job_runs (
    id                      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job                     varchar(40) NOT NULL,
    status                  varchar(20) NOT NULL,
    started_at              timestamptz NOT NULL DEFAULT now(),
    finished_at             timestamptz,
    from_date               date,
    to_date                 date,
    rows_sales              int,
    rows_stock              int,
    last_order_id           bigint,
    last_stock_movement_id  bigint,
    error                   text,
    CONSTRAINT ck_job_runs_status CHECK (status IN ('running', 'success', 'failed'))
);
"""

DOWNGRADE_SQL = """
DROP SCHEMA analytics CASCADE;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
