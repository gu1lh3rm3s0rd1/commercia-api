"""Etapa 2 of clients → people: the /people routes and people in the mobile pull. Require a
real Postgres with migrations up to 0014 applied; skipped otherwise."""

import random
import string
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, text

from app.db.session import SessionLocal, engine
from app.models.company import Company
from tests.conftest import requires_db


def _permission_applied() -> bool:
    try:
        with engine.connect() as conn:
            return conn.execute(text("SELECT 1 FROM permissions WHERE code = 'purchases.manage'")).first() is not None
    except Exception:
        return False


pytestmark = [requires_db, pytest.mark.skipif(not _permission_applied(), reason="migration 0014 not applied")]


def _digits(n: int) -> str:
    return "".join(random.choices(string.digits, k=n))


def _u() -> str:
    return uuid.uuid4().hex[:8]


def _filial(client, h) -> int:
    return client.get("/api/v1/auth/me", headers=h).json()["accessible_filial_ids"][0]


def _other_store() -> int:
    db = SessionLocal()
    try:
        other = db.execute(select(Company).where(Company.filial_code == "AUDIT2")).scalar_one_or_none()
        if other is None:
            other = Company(filial_code="AUDIT2", corporate_name="Loja Auditoria 2 LTDA", cnpj="99999999000199")
            db.add(other)
            db.commit()
        return other.id
    finally:
        db.close()


def _user(client, admin_h, f: int, profile: str) -> dict[str, str]:
    groups = {g["name"]: g["id"] for g in client.get("/api/v1/users/groups", headers=admin_h).json()}
    username = f"u{_u()}"
    client.post("/api/v1/users", json={"full_name": "Pessoa Teste", "username": username, "email": f"{username}@example.com",
                                        "password": "senha123", "group_ids": [groups[profile]], "filial_ids": [f]},
                headers=admin_h)
    token = client.post("/api/v1/auth/login", data={"username": username, "password": "senha123"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _supplier(**extra) -> dict:
    return {"person_type": "company", "roles": ["supplier"], "corporate_name": f"Distribuidora {_u()} LTDA",
            "trade_name": f"Distri {_u()}", "cnpj": _digits(14), **extra}


def _individual(**extra) -> dict:
    return {"roles": ["client"], "first_name": "João", "last_name": f"Silva {_u()}", "cpf": _digits(11), **extra}


def _create(client, h, f: int, body: dict):
    return client.post(f"/api/v1/people?filial_id={f}", json=body, headers=h)


# ---------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------


def test_register_a_company_supplier_and_a_person_with_two_roles(client, admin_headers):
    f = _filial(client, admin_headers)
    company = _create(client, admin_headers, f, _supplier())
    assert company.status_code == 201, company.text
    c = company.json()
    assert c["person_type"] == "company" and c["roles"] == ["supplier"] and c["display_name"] == c["trade_name"]
    assert c["first_name"] is None and c["email"] is None

    both = _create(client, admin_headers, f, _individual(roles=["supplier", "client", "client"])).json()
    assert both["roles"] == ["client", "supplier"]
    assert client.get(f"/api/v1/clients/{both['id']}", headers=admin_headers).status_code == 200  # a client too
    assert client.get(f"/api/v1/clients/{c['id']}", headers=admin_headers).status_code == 404  # only a supplier
    assert client.get("/api/v1/people/roles", headers=admin_headers).json() == {"client": "Cliente", "supplier": "Fornecedor"}


def test_invalid_registrations(client, admin_headers):
    f = _filial(client, admin_headers)
    for body in (_individual(cpf=None),                            # PF without CPF
                 _supplier(cnpj=None),                             # PJ without CNPJ
                 _supplier(cpf=_digits(11)),                       # PJ with CPF
                 _individual(roles=[]),                            # no role
                 _individual(roles=["comprador"]),                 # unknown role
                 _individual(cpf="123"),                           # bad CPF format
                 _individual(status="active")):                    # unknown field on create
        assert _create(client, admin_headers, f, body).status_code == 422, body


def test_documents_are_unique_per_store(client, admin_headers):
    main, other = _filial(client, admin_headers), _other_store()
    body = _supplier()
    first = _create(client, admin_headers, main, body).json()
    again = _create(client, admin_headers, main, {**body, "corporate_name": "Outra razão"})
    assert again.status_code == 409 and f"(person {first['id']})" in again.json()["detail"]
    assert _create(client, admin_headers, other, body).status_code == 201  # same supplier, another business


def test_list_filters(client, admin_headers):
    f = _filial(client, admin_headers)
    tag = _u()
    supplier = _create(client, admin_headers, f, _supplier(trade_name=f"Atacado {tag}")).json()
    _create(client, admin_headers, f, _individual(last_name=f"Cliente {tag}"))
    suppliers = client.get(f"/api/v1/people?filial_id={f}&role=supplier&search={tag}", headers=admin_headers).json()
    assert [p["id"] for p in suppliers["items"]] == [supplier["id"]]
    both = client.get(f"/api/v1/people?filial_id={f}&search={tag}", headers=admin_headers).json()
    assert both["total"] == 2
    by_doc = client.get(f"/api/v1/people?filial_id={f}&search={supplier['cnpj'][:8]}", headers=admin_headers).json()
    assert supplier["id"] in [p["id"] for p in by_doc["items"]]


def test_edit_roles_deactivate_and_reactivate(client, admin_headers):
    f = _filial(client, admin_headers)
    pid = _create(client, admin_headers, f, _supplier()).json()["id"]
    became_client = client.patch(f"/api/v1/people/{pid}", json={"roles": ["supplier", "client"]}, headers=admin_headers)
    assert became_client.json()["roles"] == ["client", "supplier"]
    assert client.get(f"/api/v1/clients/{pid}", headers=admin_headers).status_code == 200

    assert client.patch(f"/api/v1/people/{pid}", json={"cpf": _digits(11)}, headers=admin_headers).status_code == 400
    assert client.patch(f"/api/v1/people/{pid}", json={"roles": []}, headers=admin_headers).status_code == 422
    assert client.patch(f"/api/v1/people/{pid}", json={"corporate_name": None}, headers=admin_headers).status_code == 422

    assert client.delete(f"/api/v1/people/{pid}", headers=admin_headers).json()["status"] == "inactive"
    assert client.patch(f"/api/v1/people/{pid}", json={"status": "active"}, headers=admin_headers).json()["status"] == "active"

    address = client.post(f"/api/v1/people/{pid}/addresses", json={
        "street": "Av. Brasil", "city": "Ribeirão Preto", "state": "SP", "zip_code": "14000000"}, headers=admin_headers)
    assert address.status_code == 201 and address.json()["person_id"] == pid
    actions = [r["action"] for r in client.get(f"/api/v1/audit-logs?entity_type=person&entity_id={pid}",
                                               headers=admin_headers).json()["items"]]
    assert actions[-1] == "person.create" and "person.deactivate" in actions


# ---------------------------------------------------------------------
# permissions follow the roles
# ---------------------------------------------------------------------


def test_a_seller_registers_clients_but_not_suppliers(client, admin_headers):
    f = _filial(client, admin_headers)
    seller = _user(client, admin_headers, f, "Vendedor")
    mine = _create(client, seller, f, _individual())
    assert mine.status_code == 201
    assert _create(client, seller, f, _supplier()).status_code == 403
    # can't turn a client into a supplier, nor touch an existing supplier
    assert client.patch(f"/api/v1/people/{mine.json()['id']}", json={"roles": ["client", "supplier"]},
                        headers=seller).status_code == 403
    supplier = _create(client, admin_headers, f, _supplier()).json()["id"]
    assert client.patch(f"/api/v1/people/{supplier}", json={"phone": "1"}, headers=seller).status_code == 403
    assert client.delete(f"/api/v1/people/{supplier}", headers=seller).status_code == 403
    assert client.get(f"/api/v1/people/{supplier}", headers=seller).status_code == 200  # reading is fine


def test_a_manager_registers_suppliers(client, admin_headers):
    f = _filial(client, admin_headers)
    manager = _user(client, admin_headers, f, "Gerente")
    assert _create(client, manager, f, _supplier()).status_code == 201


def test_another_store_is_out_of_reach(client, admin_headers):
    other = _other_store()
    pid = _create(client, admin_headers, other, _supplier()).json()["id"]
    manager = _user(client, admin_headers, _filial(client, admin_headers), "Gerente")
    assert client.get(f"/api/v1/people/{pid}", headers=manager).status_code == 404
    assert _create(client, manager, other, _supplier()).status_code == 403


# ---------------------------------------------------------------------
# mobile pull
# ---------------------------------------------------------------------


def test_people_reach_the_app_incrementally(client, admin_headers):
    f = _filial(client, admin_headers)
    supplier = _create(client, admin_headers, f, _supplier()).json()
    full = client.get(f"/api/v1/sync/pull?filial_id={f}", headers=admin_headers).json()
    sent = next(p for p in full["people"] if p["id"] == supplier["id"])
    assert sent["roles"] == ["supplier"] and sent["display_name"] == supplier["trade_name"] and sent["status"] == "active"

    synced_at = full["synced_at"]
    _age_people(f)  # everything older than the overlap window...
    client.patch(f"/api/v1/people/{supplier['id']}", json={"phone": "16911112222"}, headers=admin_headers)
    delta = client.get("/api/v1/sync/pull", params={"filial_id": f, "last_synced_at": synced_at},
                       headers=admin_headers).json()
    assert [p["id"] for p in delta["people"]] == [supplier["id"]]  # ...so only the edited one comes back
    assert delta["people"][0]["phone"] == "16911112222"


def _age_people(filial_id: int) -> None:
    with engine.begin() as conn:
        conn.execute(text("UPDATE people SET updated_at = :old WHERE filial_id = :f"),
                     {"old": datetime(2020, 1, 1, tzinfo=timezone.utc), "f": filial_id})
