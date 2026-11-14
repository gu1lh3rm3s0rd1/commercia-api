"""chat_conversations + chat_messages: the assistant's conversations (Fase 11, RF08)

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-14

A conversation belongs to one user (only they can read it) and isn't tied to a store: a
user with several stores can ask about any of them, or compare them, in the same chat.
Messages keep the user's question, the assistant's answer and every tool call in between
(name, arguments, result) — the trace of where each number came from.

As always, written by hand and NOT run by Claude — applied manually.
"""

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE chat_conversations (
            id         bigserial PRIMARY KEY,
            user_id    bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            title      varchar(120) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_chat_conversations_user ON chat_conversations (user_id, updated_at DESC)")
    op.execute("""
        CREATE TABLE chat_messages (
            id              bigserial PRIMARY KEY,
            conversation_id bigint NOT NULL REFERENCES chat_conversations(id) ON DELETE CASCADE,
            role            varchar(10) NOT NULL CONSTRAINT ck_chat_messages_role
                                CHECK (role IN ('user', 'assistant', 'tool')),
            content         text NOT NULL DEFAULT '',
            tool_name       varchar(60),
            tool_args       jsonb,
            tool_result     jsonb,
            input_tokens    integer,
            output_tokens   integer,
            created_at      timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_chat_messages_conversation ON chat_messages (conversation_id, id)")
    op.execute("CREATE INDEX ix_chat_messages_created_at ON chat_messages (created_at)")


def downgrade() -> None:
    op.execute("DROP TABLE chat_messages")
    op.execute("DROP TABLE chat_conversations")
