"""agent_alerts + agent_summaries + permission agent.view (Fase 12, Etapa B — RF09)

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-18

The agent (Hulk plan) looks at each store every night and records what deserves attention:
- agent_alerts: one row per finding (sales outside the forecast band, stock about to run out,
  a regular seller that stopped selling, a sale below cost, a spike of cancellations, stale
  data). Numbers come from rules in code over analytics/forecasts — the AI never invents them.
  The same finding isn't recorded twice for the same day (uq_agent_alerts_dedupe).
- agent_summaries: the weekly summary, written by the model from numbers computed by the
  code (or by a template when the model is unavailable). One per store and week.
- agent.view: who sees them — Administrador and Gerente (decided 2026-11-18).

As always, written by hand and NOT run by Claude — applied manually.
"""

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE agent_alerts (
            id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            filial_id      bigint NOT NULL REFERENCES companies(id),
            kind           varchar(40) NOT NULL,
            severity       varchar(10) NOT NULL,
            entity_type    varchar(20),
            entity_id      bigint,
            reference_date date NOT NULL,
            title          varchar(160) NOT NULL,
            message        text NOT NULL,
            data           jsonb NOT NULL DEFAULT '{}'::jsonb,
            status         varchar(10) NOT NULL DEFAULT 'new',
            handled_by_id  bigint REFERENCES users(id),
            handled_at     timestamptz,
            created_at     timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_agent_alerts_severity CHECK (severity IN ('info', 'warning', 'critical')),
            CONSTRAINT ck_agent_alerts_status CHECK (status IN ('new', 'seen', 'dismissed'))
        )
    """)
    op.execute("""CREATE UNIQUE INDEX uq_agent_alerts_dedupe
                  ON agent_alerts (filial_id, kind, (COALESCE(entity_id, 0)), reference_date)""")
    op.execute("CREATE INDEX ix_agent_alerts_filial_status ON agent_alerts (filial_id, status, reference_date)")

    op.execute("""
        CREATE TABLE agent_summaries (
            id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            filial_id   bigint NOT NULL REFERENCES companies(id),
            week_start  date NOT NULL,
            week_end    date NOT NULL,
            content     text NOT NULL,
            data        jsonb NOT NULL DEFAULT '{}'::jsonb,
            generated_by varchar(60) NOT NULL,
            created_at  timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_agent_summaries_week UNIQUE (filial_id, week_start)
        )
    """)

    op.execute("""
        INSERT INTO permissions (name, code) VALUES ('Ver alertas e resumo do agente de IA', 'agent.view')
        ON CONFLICT (code) DO NOTHING
    """)
    op.execute("""
        INSERT INTO group_permissions (group_id, permission_id)
        SELECT g.id, p.id FROM groups g JOIN permissions p ON p.code = 'agent.view'
        WHERE g.name IN ('Administrador', 'Gerente')
        ON CONFLICT (group_id, permission_id) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DELETE FROM permissions WHERE code = 'agent.view'")
    op.execute("DROP TABLE agent_summaries")
    op.execute("DROP TABLE agent_alerts")
