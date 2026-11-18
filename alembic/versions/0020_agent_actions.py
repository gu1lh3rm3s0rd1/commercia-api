"""agent_actions: actions the AI consultant proposes and the user confirms with "sim" (Fase 12, Etapa C)

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-18

The agent works as a consultant (decided 2026-11-18): it analyses the store, suggests strategies
and, when a strategy is something the system can do (an offer, a price, a cost, a draft
purchase, handling alerts), it proposes ONE action and asks "Posso fazer isso? Responda sim ou
não." The CODE — not the model — recognises the answer and runs exactly the stored action, with
the permissions of the user who answered, through the same services as the panel. A proposal
expires after 15 minutes; any answer other than yes discards it. Users, profiles, passwords, the
plan and registering sales are never actions.

One pending proposal per conversation at most (uq_agent_actions_one_pending).

As always, written by hand and NOT run by Claude — applied manually.
"""

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE agent_actions (
            id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            conversation_id bigint NOT NULL REFERENCES chat_conversations(id) ON DELETE CASCADE,
            user_id         bigint NOT NULL REFERENCES users(id),
            filial_id       bigint NOT NULL REFERENCES companies(id),
            action_type     varchar(40) NOT NULL,
            params          jsonb NOT NULL,
            summary         text NOT NULL,
            status          varchar(10) NOT NULL DEFAULT 'pending',
            result          jsonb,
            created_at      timestamptz NOT NULL DEFAULT now(),
            expires_at      timestamptz NOT NULL,
            decided_at      timestamptz,
            CONSTRAINT ck_agent_actions_status CHECK (status IN ('pending', 'executed', 'declined', 'expired', 'failed'))
        )
    """)
    op.execute("CREATE UNIQUE INDEX uq_agent_actions_one_pending ON agent_actions (conversation_id) WHERE status = 'pending'")
    op.execute("CREATE INDEX ix_agent_actions_filial ON agent_actions (filial_id, created_at)")


def downgrade() -> None:
    op.execute("DROP TABLE agent_actions")
