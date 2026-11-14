"""permission purchases.manage (Administrador, Gerente)

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-14

Etapa 2 of clients → people: registering a supplier (any person role other than "client")
requires purchases.manage — the permission the purchases module (Etapa 3) will use too.
Granted to the fixed profiles Administrador and Gerente; Vendedor keeps registering clients
only (clients.edit). Same values as app/core/roles.py. Data only, idempotent.

As always, written by hand and NOT run by Claude — applied manually.
"""

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO permissions (name, code) VALUES ('Gerenciar compras e fornecedores', 'purchases.manage')
        ON CONFLICT (code) DO NOTHING
    """)
    op.execute("""
        INSERT INTO group_permissions (group_id, permission_id)
        SELECT g.id, p.id FROM groups g JOIN permissions p ON p.code = 'purchases.manage'
        WHERE g.name IN ('Administrador', 'Gerente')
        ON CONFLICT (group_id, permission_id) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DELETE FROM permissions WHERE code = 'purchases.manage'")  # cascades to group_permissions
