"""Integration tests for the Fase 2 catalog module — require a real Postgres with the
Fase 0 schema applied and scripts/seed_admin.py already run. Skipped automatically
otherwise, see the `requires_db` marker in conftest.py.

Note: these run against the real configured database (no test-DB isolation/rollback in
this project yet), so every test uses a random suffix on names/SKUs to stay re-runnable.
"""

import uuid
from decimal import Decimal

from tests.conftest import requires_db

pytestmark = requires_db


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _admin_filial_id(client, admin_headers) -> int:
    me = client.get("/api/v1/auth/me", headers=admin_headers).json()
    return me["accessible_filial_ids"][0]


def _create_department(client, admin_headers) -> int:
    resp = client.post(
        "/api/v1/catalog/departments", json={"name": _unique("Depto")}, headers=admin_headers
    )
    assert resp.status_code == 201
    return resp.json()["id"]


def _create_category(client, admin_headers, filial_id: int, department_id: int) -> int:
    resp = client.post(
        f"/api/v1/catalog/categories?filial_id={filial_id}",
        json={"name": _unique("Categoria"), "department_id": department_id},
        headers=admin_headers,
    )
    assert resp.status_code == 201
    return resp.json()["id"]


def test_department_create_and_list(client, admin_headers):
    department_id = _create_department(client, admin_headers)

    list_resp = client.get("/api/v1/catalog/departments", headers=admin_headers)
    assert list_resp.status_code == 200
    assert any(d["id"] == department_id for d in list_resp.json())


def test_category_duplicate_name_rejected(client, admin_headers):
    filial_id = _admin_filial_id(client, admin_headers)
    department_id = _create_department(client, admin_headers)
    name = _unique("Categoria")

    first = client.post(
        f"/api/v1/catalog/categories?filial_id={filial_id}",
        json={"name": name, "department_id": department_id},
        headers=admin_headers,
    )
    assert first.status_code == 201

    duplicate = client.post(
        f"/api/v1/catalog/categories?filial_id={filial_id}",
        json={"name": name, "department_id": department_id},
        headers=admin_headers,
    )
    assert duplicate.status_code == 409


def test_category_requires_filial_id(client, admin_headers):
    department_id = _create_department(client, admin_headers)
    resp = client.post(
        "/api/v1/catalog/categories",
        json={"name": _unique("Categoria"), "department_id": department_id},
        headers=admin_headers,
    )
    assert resp.status_code == 422  # filial_id is a required query param for write endpoints


def test_product_full_flow(client, admin_headers):
    filial_id = _admin_filial_id(client, admin_headers)
    department_id = _create_department(client, admin_headers)
    category_id = _create_category(client, admin_headers, filial_id, department_id)

    product_resp = client.post(
        f"/api/v1/catalog/products?filial_id={filial_id}",
        json={"sku": _unique("SKU"), "name": "Produto de teste", "category_id": category_id},
        headers=admin_headers,
    )
    assert product_resp.status_code == 201
    product = product_resp.json()
    assert product["status"] == "active"
    product_id = product["id"]

    price_resp = client.post(
        f"/api/v1/catalog/products/{product_id}/prices",
        json={"price_default": "19.90"},
        headers=admin_headers,
    )
    assert price_resp.status_code == 201

    current_price = client.get(
        f"/api/v1/catalog/products/{product_id}/prices/current", headers=admin_headers
    )
    assert current_price.status_code == 200
    assert Decimal(str(current_price.json()["price_default"])) == Decimal("19.90")

    stock_resp = client.get(f"/api/v1/catalog/products/{product_id}/stock", headers=admin_headers)
    assert stock_resp.status_code == 200
    assert stock_resp.json()["quantity"] == 0

    adjust_resp = client.post(
        f"/api/v1/catalog/products/{product_id}/stock/adjustments",
        json={"quantity_delta": 10, "reason": "restock"},
        headers=admin_headers,
    )
    assert adjust_resp.status_code == 201
    assert adjust_resp.json()["quantity"] == 10

    negative_adjust = client.post(
        f"/api/v1/catalog/products/{product_id}/stock/adjustments",
        json={"quantity_delta": -100, "reason": "loss"},
        headers=admin_headers,
    )
    assert negative_adjust.status_code == 400

    cost_resp = client.put(
        f"/api/v1/catalog/products/{product_id}/cost",
        json={"cost_value": "9.50"},
        headers=admin_headers,
    )
    assert cost_resp.status_code == 200
    assert Decimal(str(cost_resp.json()["cost_value"])) == Decimal("9.50")

    delete_resp = client.delete(f"/api/v1/catalog/products/{product_id}", headers=admin_headers)
    assert delete_resp.status_code == 200
    assert delete_resp.json()["status"] == "inactive"


def test_product_read_embeds_current_price_cost_and_stock(client, admin_headers):
    filial_id = _admin_filial_id(client, admin_headers)
    department_id = _create_department(client, admin_headers)
    category_id = _create_category(client, admin_headers, filial_id, department_id)

    # Name forced to sort first alphabetically ("!" sorts before letters/digits in the
    # DB's collation) — this dev DB has no test-DB isolation and has accumulated hundreds
    # of products across sessions (well past the route's limit=200 cap), so the list
    # assertion below would otherwise be at the mercy of where our product lands.
    create_resp = client.post(
        f"/api/v1/catalog/products?filial_id={filial_id}",
        json={"sku": _unique("SKU"), "name": f"!{_unique('produto-embutido')}", "category_id": category_id},
        headers=admin_headers,
    )
    assert create_resp.status_code == 201
    product = create_resp.json()
    product_id = product["id"]
    # Fresh product: no price/cost set yet — zero-valued rows, never null (team convention),
    # same idea as the zero-quantity synthetic stock row GET .../stock already returns.
    assert Decimal(product["current_price"]["price_default"]) == 0
    assert Decimal(product["current_price"]["price_offer"]) == 0
    assert product["current_price"]["id"] is None
    assert Decimal(product["cost"]["cost_value"]) == 0
    assert product["cost"]["updated_at"] is None
    assert product["stock"] == {
        "product_id": product_id,
        "filial_id": filial_id,
        "quantity": 0,
        "updated_at": None,
    }

    client.post(
        f"/api/v1/catalog/products/{product_id}/prices",
        json={"price_default": "19.90"},
        headers=admin_headers,
    )
    client.put(
        f"/api/v1/catalog/products/{product_id}/cost",
        json={"cost_value": "9.50"},
        headers=admin_headers,
    )
    client.post(
        f"/api/v1/catalog/products/{product_id}/stock/adjustments",
        json={"quantity_delta": 10, "reason": "restock"},
        headers=admin_headers,
    )

    get_resp = client.get(f"/api/v1/catalog/products/{product_id}", headers=admin_headers)
    assert get_resp.status_code == 200
    got = get_resp.json()
    assert Decimal(str(got["current_price"]["price_default"])) == Decimal("19.90")
    assert Decimal(str(got["cost"]["cost_value"])) == Decimal("9.50")
    assert got["stock"]["quantity"] == 10

    # same embedded data must show up in the paginated list (the batched/N+1-safe path)
    list_resp = client.get(
        f"/api/v1/catalog/products?filial_id={filial_id}&limit=5", headers=admin_headers
    )
    assert list_resp.status_code == 200
    listed = next(p for p in list_resp.json()["items"] if p["id"] == product_id)
    assert Decimal(str(listed["current_price"]["price_default"])) == Decimal("19.90")
    assert Decimal(str(listed["cost"]["cost_value"])) == Decimal("9.50")
    assert listed["stock"]["quantity"] == 10

    # PATCH and DELETE (deactivate) responses keep carrying the same embedded data
    patch_resp = client.patch(
        f"/api/v1/catalog/products/{product_id}", json={"name": "Produto renomeado"}, headers=admin_headers
    )
    assert patch_resp.status_code == 200
    patched = patch_resp.json()
    assert patched["name"] == "Produto renomeado"
    assert patched["stock"]["quantity"] == 10
    assert Decimal(str(patched["current_price"]["price_default"])) == Decimal("19.90")

    delete_resp = client.delete(f"/api/v1/catalog/products/{product_id}", headers=admin_headers)
    assert delete_resp.status_code == 200
    deleted = delete_resp.json()
    assert deleted["status"] == "inactive"
    assert deleted["stock"]["quantity"] == 10
    assert Decimal(str(deleted["cost"]["cost_value"])) == Decimal("9.50")


def test_write_endpoints_require_auth(client):
    resp = client.post("/api/v1/catalog/departments", json={"name": "x"})
    assert resp.status_code == 401


def test_price_contract_violations_rejected_as_422(client, admin_headers):
    filial_id = _admin_filial_id(client, admin_headers)
    department_id = _create_department(client, admin_headers)
    category_id = _create_category(client, admin_headers, filial_id, department_id)
    product_id = client.post(
        f"/api/v1/catalog/products?filial_id={filial_id}",
        json={"sku": _unique("SKU"), "name": "Produto", "category_id": category_id},
        headers=admin_headers,
    ).json()["id"]

    negative_price = client.post(
        f"/api/v1/catalog/products/{product_id}/prices",
        json={"price_default": "-1.00"},
        headers=admin_headers,
    )
    assert negative_price.status_code == 422

    offer_higher_than_default = client.post(
        f"/api/v1/catalog/products/{product_id}/prices",
        json={"price_default": "10.00", "price_offer": "20.00"},
        headers=admin_headers,
    )
    assert offer_higher_than_default.status_code == 422

    inverted_dates = client.post(
        f"/api/v1/catalog/products/{product_id}/prices",
        json={"price_default": "10.00", "start_date": "2026-02-01", "end_date": "2026-01-01"},
        headers=admin_headers,
    )
    assert inverted_dates.status_code == 422

    negative_cost = client.put(
        f"/api/v1/catalog/products/{product_id}/cost",
        json={"cost_value": "-5.00"},
        headers=admin_headers,
    )
    assert negative_cost.status_code == 422


# ---------------------------------------------------------------------
# GET /catalog/prices
# ---------------------------------------------------------------------


def _find_in_prices(client, admin_headers, filial_id: int, product_id: int) -> dict | None:
    """Walks every page — the dev DB has hundreds of products accumulated across runs."""
    offset = 0
    while True:
        page = client.get(
            f"/api/v1/catalog/prices?filial_id={filial_id}&limit=200&offset={offset}",
            headers=admin_headers,
        ).json()
        match = next((it for it in page["items"] if it["product_id"] == product_id), None)
        if match is not None:
            return match
        offset += page["limit"]
        if offset >= page["total"]:
            return None


def _create_product(client, admin_headers, filial_id: int) -> int:
    department_id = _create_department(client, admin_headers)
    category_id = _create_category(client, admin_headers, filial_id, department_id)
    resp = client.post(
        f"/api/v1/catalog/products?filial_id={filial_id}",
        json={"sku": _unique("SKU"), "name": _unique("produto-preco"), "category_id": category_id},
        headers=admin_headers,
    )
    assert resp.status_code == 201
    return resp.json()["id"]


def test_prices_lists_product_with_current_price(client, admin_headers):
    filial_id = _admin_filial_id(client, admin_headers)
    product_id = _create_product(client, admin_headers, filial_id)
    client.post(
        f"/api/v1/catalog/products/{product_id}/prices",
        json={"price_default": "20.00", "price_offer": "15.00"},
        headers=admin_headers,
    )

    item = _find_in_prices(client, admin_headers, filial_id, product_id)
    assert item is not None
    assert Decimal(item["price_default"]) == Decimal("20.00")
    assert Decimal(item["price_offer"]) == Decimal("15.00")
    assert item["filial_id"] == filial_id


def test_prices_product_without_price_comes_as_zero(client, admin_headers):
    filial_id = _admin_filial_id(client, admin_headers)
    product_id = _create_product(client, admin_headers, filial_id)

    item = _find_in_prices(client, admin_headers, filial_id, product_id)
    assert item is not None
    assert Decimal(item["price_default"]) == 0
    assert Decimal(item["price_offer"]) == 0
    assert item["start_date"] is None


def test_price_without_offer_is_stored_as_zero(client, admin_headers):
    filial_id = _admin_filial_id(client, admin_headers)
    product_id = _create_product(client, admin_headers, filial_id)

    for body in ({"price_default": "9.90"}, {"price_default": "9.90", "price_offer": None}):
        resp = client.post(f"/api/v1/catalog/products/{product_id}/prices", json=body, headers=admin_headers)
        assert resp.status_code == 201
        assert Decimal(resp.json()["price_offer"]) == 0


def test_missing_cost_and_price_404_explain_what_to_do(client, admin_headers):
    filial_id = _admin_filial_id(client, admin_headers)
    product_id = _create_product(client, admin_headers, filial_id)

    cost = client.get(f"/api/v1/catalog/products/{product_id}/cost", headers=admin_headers)
    assert cost.status_code == 404
    assert f"/catalog/products/{product_id}/cost" in cost.json()["detail"]

    price = client.get(f"/api/v1/catalog/products/{product_id}/prices/current", headers=admin_headers)
    assert price.status_code == 404
    assert f"/catalog/products/{product_id}/prices" in price.json()["detail"]


def test_prices_requires_auth(client):
    assert client.get("/api/v1/catalog/prices").status_code == 401
