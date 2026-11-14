"""Chat (Fase 11) end to end with a scripted FakeProvider — no Gemini call, no quota.
Require a real Postgres with migrations up to 0012 applied; skipped otherwise."""

import uuid

import pytest
from sqlalchemy import func, select, text

from app.ai import tools
from app.ai.providers import get_provider
from app.ai.providers.base import LLMUnavailable, ModelTurn, ToolCall
from app.ai.providers.fake import FakeProvider
from app.core.config import get_settings
from app.db.session import SessionLocal, engine
from app.main import app
from app.models.chat import ChatConversation
from app.models.company import Company
from app.models.user import User
from tests.conftest import requires_db


def _chat_schema_applied() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1 FROM chat_conversations LIMIT 1"))
        return True
    except Exception:
        return False


pytestmark = [requires_db, pytest.mark.skipif(not _chat_schema_applied(), reason="migration 0012 not applied")]


@pytest.fixture
def fake(monkeypatch):
    """Every test in this module talks to a FakeProvider — never to the real Gemini."""
    provider = FakeProvider([])
    app.dependency_overrides[get_provider] = lambda: provider
    yield provider
    app.dependency_overrides.pop(get_provider, None)


def _u(prefix: str = "u") -> str:
    return f"{prefix}{uuid.uuid4().hex[:8]}"


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


def _user(client, admin_h, filial_ids: list[int], profile: str = "Vendedor") -> dict[str, str]:
    groups = {g["name"]: g["id"] for g in client.get("/api/v1/users/groups", headers=admin_h).json()}
    username = _u()
    resp = client.post("/api/v1/users", json={
        "full_name": "Pessoa Chat", "username": username, "email": f"{username}@example.com", "password": "senha123",
        "group_ids": [groups[profile]], "filial_ids": filial_ids}, headers=admin_h)
    assert resp.status_code == 201, resp.text
    token = client.post("/api/v1/auth/login", data={"username": username, "password": "senha123"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _call(name: str, **args) -> ModelTurn:
    return ModelTurn(text=None, tool_calls=[ToolCall(name, args)])


def _conversations() -> int:
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(ChatConversation)).scalar()


# ---------------------------------------------------------------------
# the loop through the API
# ---------------------------------------------------------------------


def test_question_tool_answer_and_stored_trace(client, admin_headers, fake):
    seller = _user(client, admin_headers, [_filial(client, admin_headers)])
    got = {}

    def final(seen):
        got["result"] = seen[-1][0][1]  # what get_today returned, as the model receives it
        return ModelTurn(text=f"Hoje você faturou {got['result']['faturamento_hoje']}.")

    fake.script = [_call("get_today"), final]
    resp = client.post("/api/v1/chat", json={"message": "Quanto vendi hoje?"}, headers=seller)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["answer"].startswith("Hoje você faturou R$ ")
    assert body["tools_used"] == [{"name": "get_today", "args": {}, "ok": True}]
    assert "Pessoa Chat" in fake.started[0]["system"] and "BASE DE AJUDA" in fake.started[0]["system"]

    detail = client.get(f"/api/v1/chat/conversations/{body['conversation_id']}", headers=seller).json()
    assert detail["title"] == "Quanto vendi hoje?"
    assert [m["role"] for m in detail["messages"]] == ["user", "tool", "assistant"]
    assert detail["messages"][1]["tool_result"]["faturamento_hoje"] == got["result"]["faturamento_hoje"]


def test_follow_up_questions_carry_the_history(client, admin_headers, fake):
    h = _user(client, admin_headers, [_filial(client, admin_headers)])
    fake.script = [ModelTurn(text="Resposta 1"), ModelTurn(text="Resposta 2")]
    first = client.post("/api/v1/chat", json={"message": "Pergunta 1"}, headers=h).json()
    client.post("/api/v1/chat", json={"message": "E no mês anterior?", "conversation_id": first["conversation_id"]},
                headers=h)
    assert fake.started[1]["history"] == [("user", "Pergunta 1"), ("assistant", "Resposta 1")]
    listed = client.get("/api/v1/chat/conversations", headers=h).json()
    assert listed["total"] == 1 and listed["items"][0]["id"] == first["conversation_id"]


def test_conversations_are_private(client, admin_headers, fake):
    f = _filial(client, admin_headers)
    ana, bia = _user(client, admin_headers, [f]), _user(client, admin_headers, [f])
    fake.script = [ModelTurn(text="ok")]
    conv = client.post("/api/v1/chat", json={"message": "minha conversa"}, headers=ana).json()["conversation_id"]
    assert client.get(f"/api/v1/chat/conversations/{conv}", headers=bia).status_code == 404
    assert client.post("/api/v1/chat", json={"message": "x", "conversation_id": conv}, headers=bia).status_code == 404
    assert client.get(f"/api/v1/chat/conversations/{conv}", headers=admin_headers).status_code == 404  # not even admins


def test_model_unavailable_is_a_503_and_saves_nothing(client, admin_headers, fake):
    h = _user(client, admin_headers, [_filial(client, admin_headers)])
    before = _conversations()
    fake.script = [LLMUnavailable("rate_limited")]
    resp = client.post("/api/v1/chat", json={"message": "oi"}, headers=h)
    assert resp.status_code == 503 and resp.json()["reason"] == "rate_limited"
    assert _conversations() == before


def test_without_api_key_the_chat_says_not_configured(client, admin_headers, monkeypatch):
    monkeypatch.setattr(get_settings(), "gemini_api_key", None)
    resp = client.post("/api/v1/chat", json={"message": "oi"}, headers=admin_headers)
    assert resp.status_code == 503 and resp.json()["reason"] == "not_configured"


def test_per_user_rate_limit(client, admin_headers, fake, monkeypatch):
    h = _user(client, admin_headers, [_filial(client, admin_headers)])
    monkeypatch.setattr(get_settings(), "chat_rate_limit_per_minute", 2)
    fake.script = [ModelTurn(text="ok")] * 3
    assert client.post("/api/v1/chat", json={"message": "1"}, headers=h).status_code == 200
    assert client.post("/api/v1/chat", json={"message": "2"}, headers=h).status_code == 200
    assert client.post("/api/v1/chat", json={"message": "3"}, headers=h).status_code == 429


def test_message_validation(client, admin_headers, fake):
    assert client.post("/api/v1/chat", json={"message": ""}, headers=admin_headers).status_code == 422
    assert client.post("/api/v1/chat", json={"message": "x" * 2001}, headers=admin_headers).status_code == 422
    assert client.post("/api/v1/chat", json={"message": "x", "filial_id": 1}, headers=admin_headers).status_code == 422
    assert client.post("/api/v1/chat", json={"message": "x"}).status_code == 401


# ---------------------------------------------------------------------
# store isolation inside the tools
# ---------------------------------------------------------------------


def test_the_model_cannot_reach_another_store(client, admin_headers, fake):
    main, other = _filial(client, admin_headers), _other_store()
    h = _user(client, admin_headers, [main])
    seen = {}

    def final(s):
        seen["results"] = s[-1]
        return ModelTurn(text="ok")

    fake.script = [ModelTurn(text=None, tool_calls=[
        ToolCall("get_today", {"filial_id": other}),
        ToolCall("get_sales_ranking", {"filial_id": other}),
        ToolCall("compare_stores", {}),
    ]), final]
    body = client.post("/api/v1/chat", json={"message": "e a outra loja?"}, headers=h).json()
    today, ranking, stores = (r for _, r in seen["results"])
    assert "não está entre as lojas" in today["erro"] and "não está entre as lojas" in ranking["erro"]
    assert [s["filial_id"] for s in stores["lojas"]] in ([main], [])
    assert [t["ok"] for t in body["tools_used"]] == [False, False, True]


def test_an_owner_of_two_stores_sees_both(client, admin_headers, fake):
    main, other = _filial(client, admin_headers), _other_store()
    h = _user(client, admin_headers, [main, other], profile="Administrador")
    seen = {}

    def final(s):
        seen["results"] = [r for _, r in s[-1]]
        return ModelTurn(text="ok")

    fake.script = [ModelTurn(text=None, tool_calls=[
        ToolCall("get_today", {}), ToolCall("get_product_sales", {"level": "filial"}),
        ToolCall("get_product_sales", {"level": "filial", "filial_id": other, "period": "last_7_days"}),
    ]), final]
    client.post("/api/v1/chat", json={"message": "como estão minhas lojas?"}, headers=h)
    today, ambiguous, explicit = seen["results"]
    assert today["lojas"].startswith("todas as lojas")
    assert "informe filial_id" in ambiguous["erro"]
    assert "erro" not in explicit and explicit["nivel"] == "filial"


# ---------------------------------------------------------------------
# every tool against the real data
# ---------------------------------------------------------------------


def test_every_tool_runs_against_the_database(client, admin_headers):
    f = _filial(client, admin_headers)
    db = SessionLocal()
    try:
        admin = db.execute(select(User).where(User.username == "admin")).scalar_one()
        ctx = tools.ToolContext(db=db, user=admin, stores=tools.store_names(db, {f}))
        found = tools.run(ctx, ToolCall("find_entities", {"query": "a", "level": "category"}))
        assert "erro" in found  # min 2 chars
        category = tools.run(ctx, ToolCall("find_entities", {"query": "ca", "level": "category"}))["resultados"]
        calls = {
            "get_today": {}, "get_sales_summary": {"period": "last_month"},
            "get_product_sales": {"level": "filial", "period": "last_7_days"},
            "compare_sales_periods": {"level": "filial", "period": "last_week"},
            "get_sales_ranking": {"level": "category", "metric": "quantity", "limit": 3},
            "get_sales_trend": {"level": "filial", "period": "last_month"},
            "get_low_stock": {"threshold": 3}, "get_stockouts": {"period": "last_28_days"},
            "compare_stores": {"period": "this_month"}, "get_sales_by_seller": {"period": "this_year"},
            "get_data_status": {}, "find_entities": {"query": "ca", "level": "product"},
        }
        if category:
            calls["compare_sales_periods"] = {"level": "category", "entity_id": category[0]["id"], "period": "last_week"}
        for name, args in calls.items():
            result = tools.run(ctx, ToolCall(name, args))
            assert "falha interna" not in str(result), (name, result)
            assert "erro" not in result, (name, result)
        for name in ("forecast_sales", "get_forecast_accuracy"):  # depend on the forecast jobs having run
            result = tools.run(ctx, ToolCall(name, {}))
            assert "falha interna" not in str(result), (name, result)
        today = tools.run(ctx, ToolCall("get_product_sales", {"level": "filial", "period": "today"}))
        assert "dias completos" in today["erro"]
    finally:
        db.close()
