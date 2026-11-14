"""clients → people: one registry with roles (client, supplier, ...) and PF/PJ

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-14

Decided 2026-11-14:
- every registration lives in `people`; what a person is to the store is `roles` (text[],
  several at once; valid values listed in app/core/people.py — no migration per new role).
  Every existing client becomes roles = {client};
- pessoa física (cpf + first/last name) or jurídica (cnpj + corporate name, trade name
  optional); e-mail becomes optional (many suppliers have none);
- CPF/CNPJ unique PER STORE (UC004 "já cadastrado"), no longer across all stores — the same
  supplier serves many unrelated businesses (also closes audit item B11). E-mail: no
  uniqueness;
- updated_at, so the mobile pull can send people incrementally (Etapa 2).

Renames are metadata-only in Postgres: ids, data and the foreign key from
orders.client_id (now → people.id) are kept as they are. The column stays `client_id` on
orders on purpose (a sale's counterpart is its client; and the API contract doesn't change).

Fails loudly if the per-store uniqueness can't be created (it can't fail on data that was
globally unique before). As always, written by hand and NOT run by Claude — applied manually.
"""

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- rename the tables (and the names that still say "clients") ---
    op.execute("ALTER TABLE clients RENAME TO people")
    op.execute("ALTER SEQUENCE clients_id_seq RENAME TO people_id_seq")
    op.execute("ALTER INDEX clients_pkey RENAME TO people_pkey")
    op.execute("ALTER INDEX ix_clients_created_by_id RENAME TO ix_people_created_by_id")
    op.execute("ALTER INDEX ix_clients_filial_id RENAME TO ix_people_filial_id")
    op.execute("ALTER TABLE people RENAME CONSTRAINT ck_clients_cpf_format TO ck_people_cpf_format")
    op.execute("ALTER TABLE people RENAME CONSTRAINT clients_created_by_id_fkey TO people_created_by_id_fkey")
    op.execute("ALTER TABLE people RENAME CONSTRAINT clients_filial_id_fkey TO people_filial_id_fkey")

    op.execute("ALTER TABLE client_addresses RENAME TO person_addresses")
    op.execute("ALTER SEQUENCE client_addresses_id_seq RENAME TO person_addresses_id_seq")
    op.execute("ALTER INDEX client_addresses_pkey RENAME TO person_addresses_pkey")
    op.execute("ALTER TABLE person_addresses RENAME COLUMN client_id TO person_id")
    op.execute("ALTER INDEX ix_client_addresses_client_id RENAME TO ix_person_addresses_person_id")
    op.execute("ALTER TABLE person_addresses RENAME CONSTRAINT ck_client_addresses_zip_format TO ck_person_addresses_zip_format")
    op.execute("ALTER TABLE person_addresses RENAME CONSTRAINT client_addresses_client_id_fkey TO person_addresses_person_id_fkey")

    # --- PF/PJ, roles, updated_at ---
    op.execute("CREATE TYPE person_type AS ENUM ('individual', 'company')")
    op.execute("""
        ALTER TABLE people
            ADD COLUMN person_type    person_type NOT NULL DEFAULT 'individual',
            ADD COLUMN roles          text[]      NOT NULL DEFAULT '{client}',
            ADD COLUMN cnpj           varchar(14),
            ADD COLUMN corporate_name varchar(160),
            ADD COLUMN trade_name     varchar(160),
            ADD COLUMN updated_at     timestamptz NOT NULL DEFAULT now()
    """)
    # the defaults only served to fill the existing rows: from now on the API always says
    # what the person is
    op.execute("ALTER TABLE people ALTER COLUMN roles DROP DEFAULT")
    op.execute("""
        ALTER TABLE people
            ALTER COLUMN first_name DROP NOT NULL,
            ALTER COLUMN last_name  DROP NOT NULL,
            ALTER COLUMN cpf        DROP NOT NULL,
            ALTER COLUMN email      DROP NOT NULL
    """)
    op.execute("""
        ALTER TABLE people
            ADD CONSTRAINT ck_people_cnpj_format CHECK (cnpj ~ '^[0-9]{14}$'),
            ADD CONSTRAINT ck_people_roles_not_empty CHECK (cardinality(roles) >= 1),
            ADD CONSTRAINT ck_people_document CHECK (
                (person_type = 'individual' AND cpf IS NOT NULL AND cnpj IS NULL
                 AND first_name IS NOT NULL AND last_name IS NOT NULL)
                OR (person_type = 'company' AND cnpj IS NOT NULL AND cpf IS NULL AND corporate_name IS NOT NULL)
            )
    """)
    op.execute("CREATE INDEX ix_people_roles ON people USING gin (roles)")

    # --- uniqueness: per store instead of global ---
    op.execute("ALTER TABLE people DROP CONSTRAINT uq_clients_cpf")
    op.execute("ALTER TABLE people DROP CONSTRAINT uq_clients_email")
    op.execute("ALTER TABLE people ADD CONSTRAINT uq_people_filial_cpf UNIQUE (filial_id, cpf)")
    op.execute("ALTER TABLE people ADD CONSTRAINT uq_people_filial_cnpj UNIQUE (filial_id, cnpj)")


def downgrade() -> None:
    # Only possible while every person is still a plain PF client with cpf and e-mail, unique
    # across stores — fails otherwise, on purpose (no data is dropped or invented).
    op.execute("ALTER TABLE people DROP CONSTRAINT uq_people_filial_cnpj")
    op.execute("ALTER TABLE people DROP CONSTRAINT uq_people_filial_cpf")
    op.execute("ALTER TABLE people ADD CONSTRAINT uq_clients_email UNIQUE (email)")
    op.execute("ALTER TABLE people ADD CONSTRAINT uq_clients_cpf UNIQUE (cpf)")
    op.execute("DROP INDEX ix_people_roles")
    op.execute("""
        ALTER TABLE people
            DROP CONSTRAINT ck_people_document,
            DROP CONSTRAINT ck_people_roles_not_empty,
            DROP CONSTRAINT ck_people_cnpj_format
    """)
    op.execute("""
        ALTER TABLE people
            ALTER COLUMN first_name SET NOT NULL,
            ALTER COLUMN last_name  SET NOT NULL,
            ALTER COLUMN cpf        SET NOT NULL,
            ALTER COLUMN email      SET NOT NULL
    """)
    op.execute("""
        ALTER TABLE people
            DROP COLUMN updated_at, DROP COLUMN trade_name, DROP COLUMN corporate_name,
            DROP COLUMN cnpj, DROP COLUMN roles, DROP COLUMN person_type
    """)
    op.execute("DROP TYPE person_type")

    op.execute("ALTER TABLE person_addresses RENAME CONSTRAINT person_addresses_person_id_fkey TO client_addresses_client_id_fkey")
    op.execute("ALTER TABLE person_addresses RENAME CONSTRAINT ck_person_addresses_zip_format TO ck_client_addresses_zip_format")
    op.execute("ALTER INDEX ix_person_addresses_person_id RENAME TO ix_client_addresses_client_id")
    op.execute("ALTER TABLE person_addresses RENAME COLUMN person_id TO client_id")
    op.execute("ALTER INDEX person_addresses_pkey RENAME TO client_addresses_pkey")
    op.execute("ALTER SEQUENCE person_addresses_id_seq RENAME TO client_addresses_id_seq")
    op.execute("ALTER TABLE person_addresses RENAME TO client_addresses")

    op.execute("ALTER TABLE people RENAME CONSTRAINT people_filial_id_fkey TO clients_filial_id_fkey")
    op.execute("ALTER TABLE people RENAME CONSTRAINT people_created_by_id_fkey TO clients_created_by_id_fkey")
    op.execute("ALTER TABLE people RENAME CONSTRAINT ck_people_cpf_format TO ck_clients_cpf_format")
    op.execute("ALTER INDEX ix_people_filial_id RENAME TO ix_clients_filial_id")
    op.execute("ALTER INDEX ix_people_created_by_id RENAME TO ix_clients_created_by_id")
    op.execute("ALTER INDEX people_pkey RENAME TO clients_pkey")
    op.execute("ALTER SEQUENCE people_id_seq RENAME TO clients_id_seq")
    op.execute("ALTER TABLE people RENAME TO clients")
