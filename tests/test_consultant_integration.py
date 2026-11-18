"""Fase 12, Etapa C — the AI consultant: new read tools, and actions it proposes in the chat
that only run when the user answers "sim" (recognised by code). FakeProvider — no Gemini call.
Require migrations up to 0020; skipped otherwise."""

import random
import string
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from app.ai import tools
from app.ai.providers import get_provider
from app.ai.providers.base import ModelTurn, ToolCall
from app.ai.providers.fake import FakeProvider
from app.core.timezone import local_today
from app.db.session import SessionLocal, engine
from app.main import app
from app.models.user import User
from tests.conftest import requires_db


def _actions_applied() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1 FROM agent_actions LIMIT 1"))
        return True
    except Exception:
        return False


pytestmark = [requires_db, pytest.mark.skipif(not _actions_applied(), reason="migration 0020 not applied")]

PAST = "2020-01-01"


@pytest.fixture
def fake():
    provider = FakeProvider([])
    app.dependency_overrides[get_provider] = lambda: provider
    yield provider
    app.dependency_overrides.pop(get_provider, None)


def _u() -> str:
    return uuid.uuid4().hex[:8]


def _digits(n: int) -> str:
    return "".join(random.choices(string.digits, k=n))


def _filial(client, h) -> int:
    return client.get("/api/v1/auth/me", headers=h).json()["accessible_filial_ids"][0]


def _product(client, h, f: int, price="10.00", cost="4.00", stock=20) -> int:
    dep = client.post("/api/v1/catalog/departments", json={"name": f"D{_u()}"}, headers=h).json()["id"]
    cat = client.post(f"/api/v1/catalog/categories?filial_id={f}", json={"name": f"C{_u()}", "department_id": dep},
                      headers=h).json()["id"]
    pid = client.post(f"/api/v1/catalog/products?filial_id={f}", json={"sku": f"S{_u()}", "name": f"Consultor {_u()}",
                      "category_id": cat}, headers=h).json()["id"]
    client.post(f"/api/v1/catalog/products/{pid}/prices", json={"price_default": price, "start_date": PAST}, headers=h)
    client.put(f"/api/v1/catalog/products/{pid}/cost", json={"cost_value": cost}, headers=h)
    client.post(f"/api/v1/catalog/products/{pid}/stock/adjustments", json={"quantity_delta": stock, "reason": "restock"},
                headers=h)
    return pid


def _price(client, h, pid: int) -> dict:
    return client.get(f"/api/v1/catalog/products/{pid}/prices/current", headers=h).json()


def _ask(client, h, message: str, conversation_id: int | None = None) -> dict:
    body = {"message": message} if conversation_id is None else {"message": message, "conversation_id": conversation_id}
    resp = client.post("/api/v1/chat", json=body, headers=h)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _propose(fake, name: str, **args) -> None:
    """The model calls one propose_* tool, then writes its answer."""
    fake.script = [ModelTurn(text=None, tool_calls=[ToolCall(f"propose_{name}", args)]),
                   ModelTurn(text="Sugiro isso para girar o estoque.")]


def _user(client, admin_h, f: int, profile: str) -> dict[str, str]:
    groups = {g["name"]: g["id"] for g in client.get("/api/v1/users/groups", headers=admin_h).json()}
    username = f"u{_u()}"
    client.post("/api/v1/users", json={"full_name": "Pessoa Consultor", "username": username, "email": f"{username}@example.com",
                                        "password": "senha123", "group_ids": [groups[profile]], "filial_ids": [f]},
                headers=admin_h)
    token = client.post("/api/v1/auth/login", data={"username": username, "password": "senha123"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------
# propose → "sim" → runs; "não" → nothing
# ---------------------------------------------------------------------


def test_an_offer_runs_only_after_yes_and_ends_on_its_date(client, admin_headers, fake):
    h, f = admin_headers, _filial(client, admin_headers)
    pid = _product(client, h, f, price="10.00", cost="4.00")
    until = local_today() + timedelta(days=5)
    _propose(fake, "set_offer", product_id=pid, offer_price=8.5, until=until.isoformat())
    asked = _ask(client, h, "Como giro o estoque desse produto?")
    assert asked["answer"].endswith("Posso fazer isso? Responda sim ou não.")
    pending = asked["pending_action"]
    assert pending["action"] == "set_offer"
    assert "de R$ 10,00 por R$ 8,50 (15,0% de desconto)" in pending["summary"] and "margem de 52,9%" in pending["summary"]
    assert Decimal(_price(client, h, pid)["price_offer"]) == 0  # nothing done yet

    done = _ask(client, h, "sim", asked["conversation_id"])
    assert done["answer"].startswith("Feito: ") and done["pending_action"] is None
    assert Decimal(_price(client, h, pid)["price_offer"]) == Decimal("8.50")
    with engine.connect() as conn:  # after `until` the normal price comes back by itself
        after = conn.execute(text("""SELECT price_default, price_offer FROM prices WHERE product_id = :p
                                     AND start_date = :d"""), {"p": pid, "d": until + timedelta(days=1)}).one()
        assert after == (Decimal("10.00"), Decimal("0.00"))
        status = conn.execute(text("SELECT status FROM agent_actions WHERE id = :i"), {"i": pending["id"]}).scalar()
    assert status == "executed"
    logs = client.get(f"/api/v1/audit-logs?action=price.set&filial_id={f}&limit=5", headers=h).json()["items"]
    assert any(r["details"].get("product_id") == pid and r["details"].get("source") == "agent" for r in logs)


def test_no_means_nothing_and_another_question_discards_the_proposal(client, admin_headers, fake):
    h, f = admin_headers, _filial(client, admin_headers)
    pid = _product(client, h, f, price="10.00")
    _propose(fake, "set_price", product_id=pid, new_price=11.9)
    asked = _ask(client, h, "Devo subir o preço?")
    declined = _ask(client, h, "não", asked["conversation_id"])
    assert declined["answer"] == "Certo, não fiz nada."
    assert Decimal(_price(client, h, pid)["price_default"]) == Decimal("10.00")

    _propose(fake, "set_price", product_id=pid, new_price=11.9)
    again = _ask(client, h, "E agora?", asked["conversation_id"])
    fake.script = [ModelTurn(text="Hoje você vendeu bem.")]
    moved_on = _ask(client, h, "quanto vendi hoje?", again["conversation_id"])  # not a yes: dropped, model answers
    assert moved_on["answer"] == "Hoje você vendeu bem."
    late_yes = _ask(client, h, "sim", again["conversation_id"])  # nothing pending anymore → goes to the model
    assert not late_yes["answer"].startswith("Feito")
    assert Decimal(_price(client, h, pid)["price_default"]) == Decimal("10.00")


def test_an_expired_proposal_is_not_run(client, admin_headers, fake):
    h, f = admin_headers, _filial(client, admin_headers)
    pid = _product(client, h, f, price="10.00")
    _propose(fake, "set_price", product_id=pid, new_price=12)
    asked = _ask(client, h, "Sobe o preço?")
    with engine.begin() as conn:
        conn.execute(text("UPDATE agent_actions SET expires_at = :t WHERE id = :i"),
                     {"t": datetime.now(timezone.utc) - timedelta(minutes=1), "i": asked["pending_action"]["id"]})
    late = _ask(client, h, "sim", asked["conversation_id"])
    assert "expirou" in late["answer"]
    assert Decimal(_price(client, h, pid)["price_default"]) == Decimal("10.00")


def test_a_draft_purchase_is_created_pending(client, admin_headers, fake):
    h, f = admin_headers, _filial(client, admin_headers)
    pid = _product(client, h, f, cost="3.20", stock=0)
    supplier = client.post(f"/api/v1/people?filial_id={f}", json={"person_type": "company", "roles": ["supplier"],
                           "corporate_name": f"Forn {_u()} LTDA", "cnpj": _digits(14)}, headers=h).json()["id"]
    _propose(fake, "draft_purchase", supplier_id=supplier, items=[{"product_id": pid, "quantity": 39}])
    asked = _ask(client, h, "O que faço com a ruptura?")
    assert "39 un. de" in asked["pending_action"]["summary"] and "a R$ 3,20" in asked["pending_action"]["summary"]
    _ask(client, h, "pode", asked["conversation_id"])
    created = client.get(f"/api/v1/purchases?filial_id={f}&supplier_id={supplier}", headers=h).json()["items"]
    assert len(created) == 1 and created[0]["status"] == "pending" and Decimal(created[0]["total"]) == Decimal("124.80")
    assert client.get(f"/api/v1/catalog/products/{pid}/stock", headers=h).json()["quantity"] == 0  # pending: no stock in


# ---------------------------------------------------------------------
# what can't be proposed
# ---------------------------------------------------------------------


def test_bad_proposals_go_back_to_the_model_as_errors(client, admin_headers, fake):
    h, f = admin_headers, _filial(client, admin_headers)
    pid = _product(client, h, f, price="10.00")
    seen = {}

    def final(s):
        seen["results"] = [r for _, r in s[-1]]
        return ModelTurn(text="ok")

    fake.script = [ModelTurn(text=None, tool_calls=[
        ToolCall("propose_set_offer", {"product_id": pid, "offer_price": 12, "until": local_today().isoformat()}),
        ToolCall("propose_set_price", {"product_id": 999999999, "new_price": 5}),
        ToolCall("propose_set_price", {"product_id": pid, "new_price": 11}),
        ToolCall("propose_set_cost", {"product_id": pid, "cost_value": 5}),  # a second proposal in the same answer
    ]), final]
    answer = _ask(client, h, "melhore minhas vendas")
    offer_above_price, other_store, ok, second = seen["results"]
    assert "menor que o preço atual" in offer_above_price["erro"]
    assert "não encontrado" in other_store["erro"]
    assert "proposta" in ok
    assert "uma ação por vez" in second["erro"]
    assert answer["pending_action"]["action"] == "set_price"


def test_a_seller_gets_suggestions_but_no_price_proposals(client, admin_headers, fake):
    f = _filial(client, admin_headers)
    pid = _product(client, admin_headers, f)
    seller = _user(client, admin_headers, f, "Vendedor")
    seen = {}

    def final(s):
        seen["result"] = s[-1][0][1]
        return ModelTurn(text="Sugiro conversar com o gerente sobre o preço.")

    fake.script = [ModelTurn(text=None, tool_calls=[ToolCall("propose_set_price", {"product_id": pid, "new_price": 12})]),
                   final]
    answer = _ask(client, seller, "devo subir o preço?")
    assert "não tem permissão" in seen["result"]["erro"] and answer["pending_action"] is None


# ---------------------------------------------------------------------
# the new read tools
# ---------------------------------------------------------------------


def test_the_consultant_read_tools_run(client, admin_headers):
    f = _filial(client, admin_headers)
    pid = _product(client, admin_headers, f)
    db = SessionLocal()
    try:
        admin = db.execute(select(User).where(User.username == "admin")).scalar_one()
        ctx = tools.ToolContext(db=db, user=admin, stores=tools.store_names(db, {f}))
        calls = {
            "get_alerts": {}, "get_restock_suggestions": {"days": 10}, "get_margin": {"period": "last_month"},
            "get_top_clients": {"period": "last_month", "limit": 5}, "get_inactive_clients": {"days": 90},
            "get_purchases_summary": {"period": "this_year"}, "get_sales_patterns": {"period": "last_28_days"},
            "get_product_info": {"product_id": pid},
        }
        for name, args in calls.items():
            result = tools.run(ctx, ToolCall(name, args))
            assert "falha interna" not in str(result), (name, result)
            if name != "get_restock_suggestions":  # needs a forecast, may legitimately be missing
                assert "erro" not in result, (name, result)
        info = tools.run(ctx, ToolCall("get_product_info", {"product_id": pid}))
        assert info["preco_normal"] == "R$ 10,00" and info["custo"] == "R$ 4,00" and info["margem_no_preco_praticado"] == "60,0%"

        # percentages reach the model as real percentages (the dashboard reports give percent points,
        # the tools used to show 35,27 as "3527,0%")
        def pct(value: str) -> float:
            return float(value.rstrip("%").replace(".", "").replace(",", "."))

        margin = tools.run(ctx, ToolCall("get_margin", {"period": "last_month"}))
        assert 0 <= pct(margin["margem_pct"]) <= 100
        assert all(-100 <= pct(p["margem_pct"]) <= 100 for p in margin["produtos_com_menor_margem"])
        summary = tools.run(ctx, ToolCall("get_sales_summary", {"period": "last_month"}))
        assert 0 <= pct(summary["taxa_cancelamento"]) <= 100
    finally:
        db.close()
