"""users.full_name, audit_logs, fixed access profiles (RF01 / RNF11)

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-09

- users.full_name (RF01 "nome"): existing users get their username copied, then NOT NULL.
- audit_logs (RNF11): append-only trail written by app/core/audit.py in the same
  transaction as each write; logins (and failed logins) too. Reads are not logged.
- Permissions + the 3 FIXED profiles (decided 2026-11-09), idempotent — rows that already
  exist (seeded "Administrador", simulator "Vendedor") are kept and completed:
    Administrador: users.manage, products.edit, clients.edit, orders.manage, audit.view
    Gerente:       products.edit, clients.edit, orders.manage
    Vendedor:      orders.manage, clients.edit
  Same values as app/core/roles.py (repeated here on purpose: a migration must not depend
  on app code that can change later).

As always, written by hand and NOT run by Claude — applied manually.
"""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN full_name varchar(120)")
    op.execute("UPDATE users SET full_name = username")
    op.execute("ALTER TABLE users ALTER COLUMN full_name SET NOT NULL")

    op.execute("""
        CREATE TABLE audit_logs (
            id          bigserial PRIMARY KEY,
            occurred_at timestamptz NOT NULL DEFAULT now(),
            user_id     bigint REFERENCES users(id) ON DELETE SET NULL,
            username    varchar(60),
            filial_id   bigint REFERENCES companies(id) ON DELETE SET NULL,
            action      varchar(60) NOT NULL,
            entity_type varchar(40),
            entity_id   bigint,
            ip          varchar(64),
            details     jsonb NOT NULL DEFAULT '{}'::jsonb
        )
    """)
    op.execute("CREATE INDEX ix_audit_logs_occurred_at ON audit_logs (occurred_at)")
    op.execute("CREATE INDEX ix_audit_logs_user_occurred ON audit_logs (user_id, occurred_at)")
    op.execute("CREATE INDEX ix_audit_logs_filial_occurred ON audit_logs (filial_id, occurred_at)")
    op.execute("CREATE INDEX ix_audit_logs_entity ON audit_logs (entity_type, entity_id)")

    op.execute("""
        INSERT INTO permissions (name, code) VALUES
            ('Gerenciar usuários', 'users.manage'),
            ('Gerenciar catálogo (categorias/produtos/preços/estoque)', 'products.edit'),
            ('Gerenciar clientes', 'clients.edit'),
            ('Gerenciar vendas', 'orders.manage'),
            ('Consultar log de auditoria', 'audit.view')
        ON CONFLICT (code) DO NOTHING
    """)
    op.execute("""
        INSERT INTO groups (name) VALUES ('Administrador'), ('Gerente'), ('Vendedor')
        ON CONFLICT (name) DO NOTHING
    """)
    op.execute("""
        INSERT INTO group_permissions (group_id, permission_id)
        SELECT g.id, p.id
        FROM (VALUES
            ('Administrador', 'users.manage'), ('Administrador', 'products.edit'),
            ('Administrador', 'clients.edit'), ('Administrador', 'orders.manage'),
            ('Administrador', 'audit.view'),
            ('Gerente', 'products.edit'), ('Gerente', 'clients.edit'), ('Gerente', 'orders.manage'),
            ('Vendedor', 'orders.manage'), ('Vendedor', 'clients.edit')
        ) AS m(group_name, code)
        JOIN groups g ON g.name = m.group_name
        JOIN permissions p ON p.code = m.code
        ON CONFLICT (group_id, permission_id) DO NOTHING
    """)


def downgrade() -> None:
    # Profiles/permissions are left in place (users may already be assigned to them);
    # only what this migration created structurally is dropped.
    op.execute("DROP TABLE audit_logs")
    op.execute("ALTER TABLE users DROP COLUMN full_name")
