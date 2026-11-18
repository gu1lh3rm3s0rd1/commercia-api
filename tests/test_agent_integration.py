"""Fase 12, Etapa B — the AI agent's alerts and weekly summary (migration 0019). Hulk plan,
permission agent.view. No Gemini call: the summary uses a FakeProvider or the template.
Require migrations up to 0019; skipped otherwise."""

import random
import string
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.ai.providers.base import ModelTurn
from app.ai.providers.fake import FakeProvider
from app.core.timezone import local_today
from app.db.session import SessionLocal, engine
from app.forecasting import queries as fq
from app.main import app
from app.modules.agent.router import provider_or_none
from tests.conftest import requires_db


def _agent_applied() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1 FROM agent_alerts LIMIT 1"))
        return True
    except Exception:
        return False


pytestmark = [requires_db, pytest.mark.skipif(not _agent_applied(), reason="migration 0019 not applied")]

PAST = "2020-01-01"


@pytest.fixture
def no_model():
    app.dependency_overrides[provider_or_none] = lambda: None
    yield
    app.dependency_overrides.pop(provider_or_none, None)


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
    pid = client.post(f"/api/v1/catalog/products?filial_id={f}", json={"sku": f"S{_u()}", "name": f"Agente {_u()}",
                      "category_id": cat}, headers=h).json()["id"]
    client.post(f"/api/v1/catalog/products/{pid}/prices", json={"price_default": price, "start_date": PAST}, headers=h)
    client.put(f"/api/v1/catalog/products/{pid}/cost", json={"cost_value": cost}, headers=h)
    client.post(f"/api/v1/catalog/products/{pid}/stock/adjustments", json={"quantity_delta": stock, "reason": "restock"},
                headers=h)
    return pid


def _sell(client, h, f: int, pid: int, qty: int = 1) -> int:
    cid = client.post(f"/api/v1/clients?filial_id={f}", json={"first_name": "Ag", "last_name": "Cli",
                      "email": f"{_u()}@example.com", "cpf": _digits(11)}, headers=h).json()["id"]
    return client.post(f"/api/v1/orders?filial_id={f}", json={"client_id": cid, "payment_method": "cash",
                       "items": [{"product_id": pid, "quantity": qty}]}, headers=h).json()["id"]


def _alerts(client, h, f: int, **params) -> list[dict]:
    resp = client.get("/api/v1/agent/alerts", params={"filial_id": f, "limit": 200, **params}, headers=h)
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


def _run(client, h, f: int, day=None, summary=False) -> dict:
    params = {"filial_id": f, "summary": str(summary).lower()}
    if day:
        params["day"] = day.isoformat()
    resp = client.post("/api/v1/agent/run", params=params, headers=h)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _user(client, admin_h, filial_ids: list[int], profile: str) -> dict[str, str]:
    groups = {g["name"]: g["id"] for g in client.get("/api/v1/users/groups", headers=admin_h).json()}
    username = f"u{_u()}"
    client.post("/api/v1/users", json={"full_name": "Pessoa Agente", "username": username, "email": f"{username}@example.com",
                                        "password": "senha123", "group_ids": [groups[profile]], "filial_ids": filial_ids},
                headers=admin_h)
    token = client.post("/api/v1/auth/login", data={"username": username, "password": "senha123"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------
# detectors
# ---------------------------------------------------------------------


def test_a_sale_below_cost_is_flagged_once(client, admin_headers, no_model):
    h, f, today = admin_headers, _filial(client, admin_headers), local_today()
    pid = _product(client, h, f, price="10.00", cost="15.00")
    _sell(client, h, f, pid, qty=2)
    first = _run(client, h, f, day=today)
    flagged = [a for a in first["alerts_created"] if a["kind"] == "below_cost" and a["entity_id"] == pid]
    assert len(flagged) == 1 and flagged[0]["severity"] == "warning"
    assert flagged[0]["data"] == {"quantity": 2, "revenue": "20.00", "cost": "30.00"}
    assert "prejuízo de R$ 10,00" in flagged[0]["message"]
    again = _run(client, h, f, day=today)  # the same finding for the same day isn't repeated
    assert not [a for a in again["alerts_created"] if a["kind"] == "below_cost" and a["entity_id"] == pid]


def test_low_stock_against_the_forecast(client, admin_headers, no_model):
    h, f = admin_headers, _filial(client, admin_headers)
    db = SessionLocal()
    try:
        run = fq.pick_run(db, f, "quantity", None)
        if run is None:
            pytest.skip("no forecast in this database")
        start = local_today()
        candidate = db.execute(text("""
            SELECT p.id, COALESCE(s.quantity, 0) FROM forecasting.forecasts fc JOIN products p ON p.id = fc.entity_id
            LEFT JOIN stocks s ON s.product_id = p.id
            WHERE fc.run_id = :r AND fc.level = 'product' AND fc.target_date BETWEEN :a AND :b AND p.status = 'active'
            GROUP BY p.id, s.quantity HAVING avg(fc.predicted) >= 1 ORDER BY avg(fc.predicted) DESC LIMIT 1
        """), {"r": run["id"], "a": start, "b": start + timedelta(days=6)}).first()
    finally:
        db.close()
    if candidate is None:
        pytest.skip("no product with enough forecast demand")
    pid, stock = candidate
    if stock != 1:
        client.post(f"/api/v1/catalog/products/{pid}/stock/adjustments",
                    json={"quantity_delta": 1 - stock, "reason": "adjustment"}, headers=h)
    yesterday = local_today() - timedelta(days=1)
    _run(client, h, f, day=yesterday)
    risk = [a for a in _alerts(client, h, f, kind="stockout_risk", start_date=yesterday, end_date=yesterday)
            if a["entity_id"] == pid]
    assert risk and risk[0]["data"]["stock"] == 1 and risk[0]["data"]["suggested_purchase"] > 0


def test_a_spike_of_cancellations(client, admin_headers, no_model):
    h, f, today = admin_headers, _filial(client, admin_headers), local_today()
    pid = _product(client, h, f, stock=40)
    for _ in range(8):
        client.post(f"/api/v1/orders/{_sell(client, h, f, pid)}/cancel", headers=h)
    _run(client, h, f, day=today)
    spike = _alerts(client, h, f, kind="cancellation_spike", start_date=today, end_date=today)
    assert spike and spike[0]["data"]["cancelled"] >= 8


def test_sales_far_below_the_forecast(client, admin_headers, no_model):
    h, f = admin_headers, _filial(client, admin_headers)
    with engine.begin() as conn:
        day, actual = conn.execute(text("""SELECT sale_date, sum(revenue) FROM analytics.daily_sales WHERE filial_id = :f
                                           GROUP BY sale_date ORDER BY sale_date DESC LIMIT 1"""), {"f": f}).one()
    db = SessionLocal()
    try:
        best = fq.pick_run(db, f, "revenue", None)
    finally:
        db.close()
    if best is None:
        pytest.skip("no forecast in this database")
    # The point the detector itself would use (best model, made before the day). Adding a new
    # run instead would change which model counts as "best" — so this one is edited in place
    # to expect twice what was sold, and restored afterwards.
    with engine.begin() as conn:
        point = conn.execute(text("""
            SELECT f.run_id, f.predicted, f.lower, f.upper FROM forecasting.forecasts f
            JOIN forecasting.forecast_runs r ON r.id = f.run_id
            WHERE r.filial_id = :f AND r.target = 'revenue' AND r.status = 'success' AND r.model = :m
              AND r.train_end < :d AND f.level = 'filial' AND f.entity_id = :f AND f.target_date = :d
            ORDER BY (r.kind = 'forecast') DESC, r.train_end DESC, r.id DESC LIMIT 1
        """), {"f": f, "m": best["model"], "d": day}).first()
        if point is None:
            pytest.skip("no forecast made before the last sale day")
        run_id, *original = point
        conn.execute(text("""UPDATE forecasting.forecasts SET predicted = :p, lower = :lo, upper = :up
                             WHERE run_id = :r AND level = 'filial' AND entity_id = :f AND target_date = :d"""),
                     {"r": run_id, "f": f, "d": day, "p": actual * 2, "lo": actual * 3 / 2, "up": actual * 5 / 2})
        conn.execute(text("DELETE FROM agent_alerts WHERE filial_id = :f AND kind = 'sales_outside_forecast' AND reference_date = :d"),
                     {"f": f, "d": day})
    try:
        created = _run(client, h, f, day=day)["alerts_created"]
        below = [a for a in created if a["kind"] == "sales_outside_forecast"]
        assert below and "abaixo do esperado" in below[0]["title"]
    finally:
        with engine.begin() as conn:
            conn.execute(text("""UPDATE forecasting.forecasts SET predicted = :p, lower = :lo, upper = :up
                                 WHERE run_id = :r AND level = 'filial' AND entity_id = :f AND target_date = :d"""),
                         {"r": run_id, "f": f, "d": day, "p": original[0], "lo": original[1], "up": original[2]})


def test_a_store_without_data_gets_a_stale_data_alert(client, admin_headers, no_model):
    with engine.begin() as conn:
        store = conn.execute(text("SELECT id FROM companies WHERE filial_code = 'HULK2'")).scalar()
        if store is None:
            store = conn.execute(text("""INSERT INTO companies (filial_code, corporate_name, cnpj, plan)
                                         VALUES ('HULK2', 'Loja Hulk Vazia LTDA', '77777777000177', 'hulk') RETURNING id""")).scalar()
    _run(client, admin_headers, store)
    titles = {a["title"] for a in _alerts(client, admin_headers, store, kind="stale_data")}
    assert {"Análises desatualizadas", "Previsão desatualizada"} <= titles


# ---------------------------------------------------------------------
# routes: plan, permission, handling
# ---------------------------------------------------------------------


def test_who_sees_the_alerts(client, admin_headers, no_model):
    f = _filial(client, admin_headers)
    _run(client, admin_headers, f)
    manager, seller = _user(client, admin_headers, [f], "Gerente"), _user(client, admin_headers, [f], "Vendedor")
    alerts = _alerts(client, manager, f)
    assert alerts  # the manager sees them
    assert client.get(f"/api/v1/agent/alerts?filial_id={f}", headers=seller).status_code == 403
    with engine.begin() as conn:
        pro = conn.execute(text("SELECT id FROM companies WHERE filial_code = 'PRO1'")).scalar()
        if pro is None:
            pro = conn.execute(text("""INSERT INTO companies (filial_code, corporate_name, cnpj, plan)
                                       VALUES ('PRO1', 'Loja Plano Pro LTDA', '88888888000188', 'pro') RETURNING id""")).scalar()
        conn.execute(text("UPDATE companies SET plan = 'pro' WHERE id = :p"), {"p": pro})
    pro_manager = _user(client, admin_headers, [pro], "Gerente")
    resp = client.get("/api/v1/agent/alerts", headers=pro_manager)
    assert resp.status_code == 403 and resp.json()["required_plan"] == "hulk"


def test_marking_an_alert_as_seen_or_dismissed(client, admin_headers, no_model):
    h, f = admin_headers, _filial(client, admin_headers)
    pid = _product(client, h, f, price="10.00", cost="15.00")
    _sell(client, h, f, pid)
    alert = next(a for a in _run(client, h, f, day=local_today())["alerts_created"] if a["entity_id"] == pid)
    me = client.get("/api/v1/auth/me", headers=h).json()["id"]
    seen = client.patch(f"/api/v1/agent/alerts/{alert['id']}", json={"status": "seen"}, headers=h).json()
    assert seen["status"] == "seen" and seen["handled_by_id"] == me and seen["handled_at"]
    assert client.patch(f"/api/v1/agent/alerts/{alert['id']}", json={"status": "done"}, headers=h).status_code == 422
    assert alert["id"] not in [a["id"] for a in _alerts(client, h, f, status="new")]


# ---------------------------------------------------------------------
# weekly summary
# ---------------------------------------------------------------------


def test_the_weekly_summary_is_written_by_the_model_from_computed_numbers(client, admin_headers):
    h, f = admin_headers, _filial(client, admin_headers)
    seen = {}

    def write(s):
        seen["numbers"] = s[-1]
        return ModelTurn(text="Semana boa: faturamento em alta.")

    provider = FakeProvider([write])
    app.dependency_overrides[provider_or_none] = lambda: provider
    try:
        summary = _run(client, h, f, summary=True)["summary"]
    finally:
        app.dependency_overrides.pop(provider_or_none, None)
    assert summary["content"] == "Semana boa: faturamento em alta." and summary["generated_by"] == "fake"
    assert '"faturamento"' in seen["numbers"] and "R$" in seen["numbers"]  # the model got numbers, ready-made
    assert client.get(f"/api/v1/agent/summary?filial_id={f}", headers=h).json()["id"] == summary["id"]


def test_without_the_model_the_summary_falls_back_to_a_template(client, admin_headers, no_model):
    h, f = admin_headers, _filial(client, admin_headers)
    summary = _run(client, h, f, summary=True)["summary"]
    assert summary["generated_by"] == "template"
    assert summary["content"].startswith("Na semana de ") and "faturou R$ " in summary["content"]
