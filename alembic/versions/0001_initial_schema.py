"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-09-09

This migration is written by hand (raw SQL via op.execute), not via
--autogenerate, because it was never run against a live database — the DDL
below is exactly what was reviewed and approved in the Fase 0 architecture
discussion. Do NOT run `alembic upgrade` until the target database exists and
its connection string is set in .env.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


UPGRADE_SQL = """
-- =====================================================================
-- Commercia — schema inicial (Fase 0)
-- =====================================================================

CREATE TYPE record_status AS ENUM ('active', 'inactive');
CREATE TYPE order_status AS ENUM ('pending', 'paid', 'cancelled');
CREATE TYPE payment_method AS ENUM ('cash', 'debit_card', 'credit_card', 'pix', 'other');
CREATE TYPE stock_movement_reason AS ENUM ('sale', 'adjustment', 'loss', 'return', 'restock');

-- ---------------------------------------------------------------------
-- departments (compartilhado entre todas as filiais)
-- ---------------------------------------------------------------------
CREATE TABLE departments (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name       varchar(120) NOT NULL,
    CONSTRAINT uq_departments_name UNIQUE (name)
);

-- ---------------------------------------------------------------------
-- companies (cada linha = uma loja/filial da rede)
-- ---------------------------------------------------------------------
CREATE TABLE companies (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    filial_code     varchar(20)  NOT NULL,
    corporate_name  varchar(160) NOT NULL,
    fantasy_name    varchar(160),
    cnpj            varchar(14)  NOT NULL,
    created_at      timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT uq_companies_filial_code UNIQUE (filial_code),
    CONSTRAINT uq_companies_cnpj UNIQUE (cnpj),
    CONSTRAINT ck_companies_cnpj_format CHECK (cnpj ~ '^[0-9]{14}$')
);
COMMENT ON TABLE companies IS 'Cada linha representa uma loja/filial. Um usuário pode acessar N filiais via user_companies.';

-- ---------------------------------------------------------------------
-- users
-- ---------------------------------------------------------------------
CREATE TABLE users (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    username       varchar(60)  NOT NULL,
    email          varchar(160) NOT NULL,
    password_hash  text         NOT NULL,
    is_active      boolean      NOT NULL DEFAULT true,
    is_superuser   boolean      NOT NULL DEFAULT false,
    created_at     timestamptz  NOT NULL DEFAULT now(),
    last_login     timestamptz,
    CONSTRAINT uq_users_username UNIQUE (username),
    CONSTRAINT uq_users_email UNIQUE (email)
);

-- ---------------------------------------------------------------------
-- groups / permissions / RBAC
-- ---------------------------------------------------------------------
CREATE TABLE groups (
    id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name  varchar(80) NOT NULL,
    CONSTRAINT uq_groups_name UNIQUE (name)
);

CREATE TABLE permissions (
    id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name  varchar(120) NOT NULL,
    code  varchar(60)  NOT NULL,
    CONSTRAINT uq_permissions_code UNIQUE (code)
);

CREATE TABLE user_groups (
    id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id   bigint NOT NULL REFERENCES users(id)  ON DELETE CASCADE,
    group_id  bigint NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    CONSTRAINT uq_user_groups UNIQUE (user_id, group_id)
);
CREATE INDEX ix_user_groups_group_id ON user_groups(group_id);

CREATE TABLE group_permissions (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    group_id       bigint NOT NULL REFERENCES groups(id)      ON DELETE CASCADE,
    permission_id  bigint NOT NULL REFERENCES permissions(id) ON DELETE CASCADE,
    CONSTRAINT uq_group_permissions UNIQUE (group_id, permission_id)
);
CREATE INDEX ix_group_permissions_permission_id ON group_permissions(permission_id);

-- ---------------------------------------------------------------------
-- user_companies (modelo de tenant N:N)
-- ---------------------------------------------------------------------
CREATE TABLE user_companies (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id    bigint NOT NULL REFERENCES users(id)     ON DELETE CASCADE,
    filial_id  bigint NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    CONSTRAINT uq_user_companies UNIQUE (user_id, filial_id)
);
CREATE INDEX ix_user_companies_filial_id ON user_companies(filial_id);
COMMENT ON TABLE user_companies IS 'Um usuário (ex.: administrador) pode ter acesso a N filiais da mesma rede.';

-- ---------------------------------------------------------------------
-- categories (por loja)
-- ---------------------------------------------------------------------
CREATE TABLE categories (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name           varchar(120) NOT NULL,
    department_id  bigint NOT NULL REFERENCES departments(id),
    company_id     bigint NOT NULL REFERENCES companies(id),
    created_by_id  bigint REFERENCES users(id),
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_categories_department_id ON categories(department_id);
CREATE INDEX ix_categories_company_id    ON categories(company_id);
CREATE INDEX ix_categories_created_by_id ON categories(created_by_id);

-- ---------------------------------------------------------------------
-- products (catálogo por loja)
-- ---------------------------------------------------------------------
CREATE TABLE products (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sku            varchar(60)  NOT NULL,
    name           varchar(200) NOT NULL,
    description    text,
    brand          varchar(120),
    barcode        varchar(30),
    category_id    bigint NOT NULL REFERENCES categories(id),
    filial_id      bigint NOT NULL REFERENCES companies(id),
    weight         numeric(10,3) CHECK (weight >= 0),
    height         numeric(10,2) CHECK (height >= 0),
    width          numeric(10,2) CHECK (width  >= 0),
    depth          numeric(10,2) CHECK (depth  >= 0),
    status         record_status NOT NULL DEFAULT 'active',
    created_by_id  bigint NOT NULL REFERENCES users(id),
    created_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_products_sku     UNIQUE (sku),
    CONSTRAINT uq_products_barcode UNIQUE (barcode)
);
CREATE INDEX ix_products_category_id    ON products(category_id);
CREATE INDEX ix_products_filial_id      ON products(filial_id);
CREATE INDEX ix_products_created_by_id  ON products(created_by_id);
COMMENT ON COLUMN products.filial_id IS
    'Invariante de negócio (não garantida pelo banco, garantida pelo service da API): '
    'deve ser igual a categories.company_id da category_id referenciada.';

-- ---------------------------------------------------------------------
-- product_costs (1:1 com products)
-- ---------------------------------------------------------------------
CREATE TABLE product_costs (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    product_id  bigint NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    cost_value  numeric(12,2) NOT NULL CHECK (cost_value >= 0),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_product_costs_product_id UNIQUE (product_id)
);

-- ---------------------------------------------------------------------
-- prices (log append-only — disciplina garantida pelo service, não pelo banco)
-- ---------------------------------------------------------------------
CREATE TABLE prices (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    product_id     bigint NOT NULL REFERENCES products(id)  ON DELETE CASCADE,
    filial_id      bigint NOT NULL REFERENCES companies(id),
    price_default  numeric(12,2) NOT NULL CHECK (price_default >= 0),
    price_offer    numeric(12,2) CHECK (price_offer >= 0),
    start_date     date,
    end_date       date,
    created_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_prices_offer_lower CHECK (price_offer IS NULL OR price_offer <= price_default),
    CONSTRAINT ck_prices_date_range  CHECK (end_date IS NULL OR start_date IS NULL OR end_date >= start_date)
);
CREATE INDEX ix_prices_product_id ON prices(product_id);
CREATE INDEX ix_prices_filial_id  ON prices(filial_id);
COMMENT ON TABLE prices IS
    'Log append-only: alterar preço = INSERT de nova linha fechando o end_date da anterior, nunca UPDATE. '
    'filial_id deve ser igual a products.filial_id do product_id — garantido pelo service, não pelo banco.';

-- ---------------------------------------------------------------------
-- stocks
-- ---------------------------------------------------------------------
CREATE TABLE stocks (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    product_id  bigint NOT NULL REFERENCES products(id)  ON DELETE CASCADE,
    filial_id   bigint NOT NULL REFERENCES companies(id),
    quantity    integer NOT NULL DEFAULT 0 CHECK (quantity >= 0),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_stocks_product_filial UNIQUE (product_id, filial_id)
);
CREATE INDEX ix_stocks_filial_id ON stocks(filial_id);
COMMENT ON TABLE stocks IS
    'Estado atual (snapshot). Histórico real vive em stock_movements — nunca alterar quantity '
    'sem gravar a movimentação correspondente na mesma transação.';

-- ---------------------------------------------------------------------
-- clients
-- ---------------------------------------------------------------------
CREATE TABLE clients (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    first_name     varchar(120) NOT NULL,
    last_name      varchar(120) NOT NULL,
    email          varchar(160) NOT NULL,
    cpf            varchar(11)  NOT NULL,
    birth_date     date,
    phone          varchar(20),
    status         record_status NOT NULL DEFAULT 'active',
    created_by_id  bigint REFERENCES users(id),
    filial_id      bigint NOT NULL REFERENCES companies(id),
    created_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_clients_cpf   UNIQUE (cpf),
    CONSTRAINT uq_clients_email UNIQUE (email),
    CONSTRAINT ck_clients_cpf_format CHECK (cpf ~ '^[0-9]{11}$')
);
CREATE INDEX ix_clients_created_by_id ON clients(created_by_id);
CREATE INDEX ix_clients_filial_id     ON clients(filial_id);

-- ---------------------------------------------------------------------
-- client_addresses
-- ---------------------------------------------------------------------
CREATE TABLE client_addresses (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    client_id     bigint NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    street        varchar(200) NOT NULL,
    number        varchar(20),
    complement    varchar(120),
    neighborhood  varchar(120),
    city          varchar(120) NOT NULL,
    state         char(2) NOT NULL,
    zip_code      varchar(8) NOT NULL,
    CONSTRAINT ck_client_addresses_zip_format CHECK (zip_code ~ '^[0-9]{8}$')
);
CREATE INDEX ix_client_addresses_client_id ON client_addresses(client_id);

-- ---------------------------------------------------------------------
-- orders
-- ---------------------------------------------------------------------
CREATE TABLE orders (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    client_id       bigint NOT NULL REFERENCES clients(id),
    total           numeric(12,2) NOT NULL DEFAULT 0 CHECK (total >= 0),
    discount        numeric(12,2) NOT NULL DEFAULT 0 CHECK (discount >= 0),
    payment_method  payment_method NOT NULL,
    status          order_status NOT NULL DEFAULT 'pending',
    created_by_id   bigint REFERENCES users(id),
    filial_id       bigint NOT NULL REFERENCES companies(id),
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_orders_client_id      ON orders(client_id);
CREATE INDEX ix_orders_created_by_id  ON orders(created_by_id);
CREATE INDEX ix_orders_filial_id      ON orders(filial_id);
COMMENT ON COLUMN orders.total IS
    'Snapshot do total já com desconto aplicado (SUM(order_items.total) - discount). '
    'A aplicação deve recalculá-lo sempre que os itens forem inseridos — nunca editado à mão.';

-- ---------------------------------------------------------------------
-- order_items
-- ---------------------------------------------------------------------
CREATE TABLE order_items (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id      bigint NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    product_id    bigint NOT NULL REFERENCES products(id),
    quantity      integer NOT NULL CHECK (quantity > 0),
    original_qty  integer CHECK (original_qty IS NULL OR original_qty >= quantity),
    unit_price    numeric(12,2) NOT NULL CHECK (unit_price >= 0),
    cost_at_sale  numeric(12,2) NOT NULL CHECK (cost_at_sale >= 0),
    total         numeric(12,2) GENERATED ALWAYS AS (quantity * unit_price) STORED,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_order_items_order_id   ON order_items(order_id);
CREATE INDEX ix_order_items_product_id ON order_items(product_id);
COMMENT ON COLUMN order_items.quantity IS 'Quantidade efetivamente atendida.';
COMMENT ON COLUMN order_items.original_qty IS
    'Preenchido só durante a sincronização mobile (Fase 5), quando o estoque real no servidor '
    'é menor que o solicitado offline: guarda o que o vendedor originalmente pediu, para relatório '
    'de vendas perdidas por ruptura. NULL quando quantity == o que foi pedido (caso normal).';
COMMENT ON COLUMN order_items.cost_at_sale IS
    'Snapshot de product_costs.cost_value no momento da venda — necessário para margem correta '
    'mesmo que o custo do produto mude depois.';

-- ---------------------------------------------------------------------
-- stock_movements (ledger append-only)
-- ---------------------------------------------------------------------
CREATE TABLE stock_movements (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    product_id     bigint NOT NULL REFERENCES products(id),
    filial_id      bigint NOT NULL REFERENCES companies(id),
    change_qty     integer NOT NULL CHECK (change_qty <> 0),
    reason         stock_movement_reason NOT NULL,
    resulting_qty  integer NOT NULL CHECK (resulting_qty >= 0),
    order_item_id  bigint REFERENCES order_items(id) ON DELETE SET NULL,
    occurred_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_stock_movements_product_id    ON stock_movements(product_id);
CREATE INDEX ix_stock_movements_filial_id     ON stock_movements(filial_id);
CREATE INDEX ix_stock_movements_order_item_id ON stock_movements(order_item_id);
COMMENT ON TABLE stock_movements IS
    'Fato histórico, não deriva de nada — é o que a camada de Analytics/Forecasting usa para '
    'distinguir queda de demanda de ruptura de estoque. Nunca fazer UPDATE em stocks.quantity '
    'sem gravar aqui na mesma transação.';
"""

DOWNGRADE_SQL = """
DROP TABLE IF EXISTS stock_movements;
DROP TABLE IF EXISTS order_items;
DROP TABLE IF EXISTS orders;
DROP TABLE IF EXISTS client_addresses;
DROP TABLE IF EXISTS clients;
DROP TABLE IF EXISTS stocks;
DROP TABLE IF EXISTS prices;
DROP TABLE IF EXISTS product_costs;
DROP TABLE IF EXISTS products;
DROP TABLE IF EXISTS categories;
DROP TABLE IF EXISTS user_companies;
DROP TABLE IF EXISTS group_permissions;
DROP TABLE IF EXISTS user_groups;
DROP TABLE IF EXISTS permissions;
DROP TABLE IF EXISTS groups;
DROP TABLE IF EXISTS users;
DROP TABLE IF EXISTS companies;
DROP TABLE IF EXISTS departments;
DROP TYPE IF EXISTS stock_movement_reason;
DROP TYPE IF EXISTS payment_method;
DROP TYPE IF EXISTS order_status;
DROP TYPE IF EXISTS record_status;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
