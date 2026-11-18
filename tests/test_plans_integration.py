"""Fase 12, Etapa A — subscription plans per store (migration 0018). Pro and Hulk both have the
app, dashboards and analytics; only Hulk has the Assistente IA (chat + agent). Require
migrations up to 0018; skipped otherwise. The chat runs on a FakeProvider (no Gemini call)."""

import uuid

import pytest
from sqlalchemy import text

from app.ai.providers import get_provider
from app.ai.providers.base import ModelTurn, ToolCall
from app.ai.providers.fake import FakeProvider
from app.db.session import engine
from app.main import app
from tests.conftest import requires_db


def _plans_applied() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT plan FROM companies LIMIT 1"))
        return True
    except Exception:
        return False


pytestmark = [requires_db, pytest.mark.skipif(not _plans_applied(), reason="migration 0018 not applied")]


@pytest.fixture
def fake():
    provider = FakeProvider([])
    app.dependency_overrides[get_provider] = lambda: provider
    yield provider
    app.dependency_overrides.pop(get_provider, None)


def _u() -> str:
    return uuid.uuid4().hex[:8]


def _filial(client, h) -> int:
    return client.get("/api/v1/auth/me", headers=h).json()["accessible_filial_ids"][0]


def _pro_store() -> int:
    """A store on the Pro plan, reset to Pro at every call (another test may upgrade it)."""
    with engine.begin() as conn:
        row = conn.execute(text("SELECT id FROM companies WHERE filial_code = 'PRO1'")).first()
        if row is None:
            row = conn.execute(text("""INSERT INTO companies (filial_code, corporate_name, cnpj, plan)
                                       VALUES ('PRO1', 'Loja Plano Pro LTDA', '88888888000188', 'pro') RETURNING id""")).first()
        conn.execute(text("UPDATE companies SET plan = 'pro' WHERE id = :i"), {"i": row[0]})
        return row[0]


def _admin_of(client, admin_h, filial_ids: list[int]) -> dict[str, str]:
    groups = {g["name"]: g["id"] for g in client.get("/api/v1/users/groups", headers=admin_h).json()}
    username = f"u{_u()}"
    client.post("/api/v1/users", json={"full_name": "Dono Teste", "username": username, "email": f"{username}@example.com",
                                        "password": "senha123", "group_ids": [groups["Administrador"]],
                                        "filial_ids": filial_ids}, headers=admin_h)
    token = client.post("/api/v1/auth/login", data={"username": username, "password": "senha123"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_companies_show_their_plan_and_features(client, admin_headers):
    main = _filial(client, admin_headers)
    pro = _pro_store()
    stores = {c["id"]: c for c in client.get("/api/v1/companies", headers=admin_headers).json()}
    assert stores[main]["plan"] == "hulk" and stores[main]["features"] == ["ai_assistant"]
    assert stores[pro]["plan"] == "pro" and stores[pro]["features"] == []


def test_a_pro_store_has_everything_but_the_assistant(client, admin_headers, fake):
    pro = _pro_store()
    h = _admin_of(client, admin_headers, [pro])
    for path in ("/api/v1/chat/conversations",):
        resp = client.get(path, headers=h)
        assert resp.status_code == 403
        assert resp.json() == {"detail": "Plano insuficiente: Assistente IA faz parte do plano Hulk.",
                               "feature": "ai_assistant", "required_plan": "hulk"}
    assert client.post("/api/v1/chat", json={"message": "oi"}, headers=h).status_code == 403
    # dashboards and "análise inteligente" are in both plans
    assert client.get(f"/api/v1/dashboard/indicators?filial_id={pro}", headers=h).status_code == 200
    assert client.get(f"/api/v1/analytics/status?filial_id={pro}", headers=h).status_code == 200


def test_upgrading_the_store_unlocks_the_assistant_and_is_audited(client, admin_headers, fake):
    pro = _pro_store()
    h = _admin_of(client, admin_headers, [pro])
    changed = client.patch(f"/api/v1/companies/{pro}/plan", json={"plan": "hulk"}, headers=admin_headers)
    assert changed.status_code == 200 and changed.json()["features"] == ["ai_assistant"]
    fake.script = [ModelTurn(text="ok")]
    assert client.post("/api/v1/chat", json={"message": "oi"}, headers=h).status_code == 200

    logs = client.get(f"/api/v1/audit-logs?action=company.plan_change&entity_id={pro}", headers=admin_headers).json()
    assert logs["items"][0]["details"] == {"plan": ["pro", "hulk"]}


def test_only_a_superuser_changes_the_plan(client, admin_headers):
    pro = _pro_store()
    h = _admin_of(client, admin_headers, [pro])
    assert client.patch(f"/api/v1/companies/{pro}/plan", json={"plan": "hulk"}, headers=h).status_code == 403
    assert client.patch(f"/api/v1/companies/{pro}/plan", json={"plan": "gold"}, headers=admin_headers).status_code == 422
    assert client.patch("/api/v1/companies/999999/plan", json={"plan": "hulk"}, headers=admin_headers).status_code == 404


def test_an_owner_with_a_hulk_and_a_pro_store(client, admin_headers, fake):
    """The assistant works (one store is Hulk) but only sees the Hulk store, and says why
    when asked about the Pro one."""
    main, pro = _filial(client, admin_headers), _pro_store()
    h = _admin_of(client, admin_headers, [main, pro])
    seen = {}

    def final(s):
        seen["results"] = [r for _, r in s[-1]]
        return ModelTurn(text="ok")

    fake.script = [ModelTurn(text=None, tool_calls=[ToolCall("get_today", {}), ToolCall("get_today", {"filial_id": pro})]),
                   final]
    assert client.post("/api/v1/chat", json={"message": "e minhas lojas?"}, headers=h).status_code == 200
    everything, the_pro_one = seen["results"]
    assert "todas as lojas" not in everything["lojas"]  # only the Hulk store is summed
    assert "plano Pro" in the_pro_one["erro"]
