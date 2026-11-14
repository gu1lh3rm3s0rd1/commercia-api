"""Etapa 1 of clients → people (migration 0013): /clients keeps its contract on top of
`people`, filtered by the "client" role. People with other roles are written directly in
the DB here (the /people routes are Etapa 2). Require a real Postgres with migrations up to
0013 applied; skipped otherwise."""

import random
import string
import uuid

import pytest
from sqlalchemy import select, text

from app.core.timezone import local_today
from app.db.session import SessionLocal, engine
from app.models.company import Company
from app.models.enums import PersonType
from app.models.sales import Person
from tests.conftest import requires_db


def _people_applied() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT roles FROM people LIMIT 1"))
        return True
    except Exception:
        return False


pytestmark = [requires_db, pytest.mark.skipif(not _people_applied(), reason="migration 0013 not applied")]

PAST = "2020-01-01"


def _digits(n: int) -> str:
    return "".join(random.choices(string.digits, k=n))


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


def _person(filial_id: int, **fields) -> int:
    db = SessionLocal()
    try:
        person = Person(filial_id=filial_id, **fields)
        db.add(person)
        db.commit()
        return person.id
    finally:
        db.close()


def _new_client(client, h, f: int, cpf: str | None = None):
    return client.post(f"/api/v1/clients?filial_id={f}", json={
        "first_name": "Ana", "last_name": "Pessoa", "email": f"{uuid.uuid4().hex[:8]}@example.com",
        "cpf": cpf or _digits(11)}, headers=h)


def _product(client, h, f: int) -> int:
    dep = client.post("/api/v1/catalog/departments", json={"name": f"D{uuid.uuid4().hex[:8]}"}, headers=h).json()["id"]
    cat = client.post(f"/api/v1/catalog/categories?filial_id={f}", json={"name": f"C{uuid.uuid4().hex[:8]}",
                      "department_id": dep}, headers=h).json()["id"]
    pid = client.post(f"/api/v1/catalog/products?filial_id={f}", json={"sku": f"S{uuid.uuid4().hex[:8]}",
                      "name": "Prod pessoas", "category_id": cat}, headers=h).json()["id"]
    client.post(f"/api/v1/catalog/products/{pid}/prices", json={"price_default": "10.00", "start_date": PAST}, headers=h)
    client.post(f"/api/v1/catalog/products/{pid}/stock/adjustments", json={"quantity_delta": 5, "reason": "restock"},
                headers=h)
    return pid


def test_a_client_created_by_clients_is_a_person_with_the_client_role(client, admin_headers):
    body = _new_client(client, admin_headers, _filial(client, admin_headers)).json()
    assert body["roles"] == ["client"] and body["person_type"] == "individual"
    assert body["cnpj"] is None and body["corporate_name"] is None and body["updated_at"]
    with engine.connect() as conn:
        assert conn.execute(text("SELECT roles FROM people WHERE id = :i"), {"i": body["id"]}).scalar() == ["client"]


def test_cpf_is_unique_per_store_not_across_stores(client, admin_headers):
    main, other = _filial(client, admin_headers), _other_store()
    cpf = _digits(11)
    assert _new_client(client, admin_headers, main, cpf).status_code == 201
    same_store = _new_client(client, admin_headers, main, cpf)
    assert same_store.status_code == 409 and same_store.json()["detail"] == "CPF already registered in this filial"
    assert _new_client(client, admin_headers, other, cpf).status_code == 201


def test_a_supplier_is_not_a_client(client, admin_headers):
    f = _filial(client, admin_headers)
    supplier = _person(f, person_type=PersonType.COMPANY, roles=["supplier"], corporate_name="Distribuidora Teste LTDA",
                       cnpj=_digits(14))
    assert client.get(f"/api/v1/clients/{supplier}", headers=admin_headers).status_code == 404
    page = client.get(f"/api/v1/clients?filial_id={f}&limit=200", headers=admin_headers).json()
    assert all("client" in c["roles"] for c in page["items"])
    sale = client.post(f"/api/v1/orders?filial_id={f}", json={
        "client_id": supplier, "payment_method": "cash", "items": [{"product_id": _product(client, admin_headers, f),
                                                                    "quantity": 1}]}, headers=admin_headers)
    assert sale.status_code == 400 and sale.json()["detail"] == "Client not found in this filial"


def test_a_company_that_is_also_a_client_buys_and_shows_by_trade_name(client, admin_headers):
    f = _filial(client, admin_headers)
    trade = f"Bar do Zé {uuid.uuid4().hex[:6]}"
    buyer = _person(f, person_type=PersonType.COMPANY, roles=["client", "supplier"], corporate_name="Zé Bebidas LTDA",
                    trade_name=trade, cnpj=_digits(14))
    read = client.get(f"/api/v1/clients/{buyer}", headers=admin_headers).json()
    assert read["first_name"] is None and read["cpf"] is None and read["trade_name"] == trade
    assert sorted(read["roles"]) == ["client", "supplier"]
    sale = client.post(f"/api/v1/orders?filial_id={f}", json={
        "client_id": buyer, "payment_method": "pix", "items": [{"product_id": _product(client, admin_headers, f),
                                                                "quantity": 1}]}, headers=admin_headers)
    assert sale.status_code == 201
    today = local_today().isoformat()  # today only: the dev DB has hundreds of bigger clients
    report = client.get(f"/api/v1/dashboard/reports/sales-by-client?filial_id={f}&start_date={today}&end_date={today}"
                        "&limit=200", headers=admin_headers).json()
    assert next(i for i in report["items"] if i["client_id"] == buyer)["client_name"] == trade
    assert client.patch(f"/api/v1/clients/{buyer}", json={"cpf": _digits(11)}, headers=admin_headers).status_code == 400


def test_the_database_enforces_the_document_rules(client, admin_headers):
    f = _filial(client, admin_headers)
    with pytest.raises(Exception):  # PJ without CNPJ
        _person(f, person_type=PersonType.COMPANY, roles=["supplier"], corporate_name="Sem CNPJ LTDA")
    with pytest.raises(Exception):  # no role at all
        _person(f, person_type=PersonType.INDIVIDUAL, roles=[], first_name="A", last_name="B", cpf=_digits(11))


def test_addresses_keep_the_client_id_field(client, admin_headers):
    cid = _new_client(client, admin_headers, _filial(client, admin_headers)).json()["id"]
    created = client.post(f"/api/v1/clients/{cid}/addresses", json={
        "street": "Rua A", "city": "Ribeirão Preto", "state": "SP", "zip_code": "14000000"}, headers=admin_headers)
    assert created.status_code == 201 and created.json()["client_id"] == cid


def test_person_changes_are_audited_as_person(client, admin_headers):
    cid = _new_client(client, admin_headers, _filial(client, admin_headers)).json()["id"]
    client.patch(f"/api/v1/clients/{cid}", json={"phone": "16999990000"}, headers=admin_headers)
    actions = [r["action"] for r in client.get(f"/api/v1/audit-logs?entity_type=person&entity_id={cid}",
                                               headers=admin_headers).json()["items"]]
    assert actions == ["person.update", "person.create"]
