"""Simulates 12 months of a store's operation through the real API routes, so the local
database has a realistic history to build Fase 7 (analytics) and Fase 8 (forecasting) on.

Refuses to run unless DB_HOST is localhost. Run from the project root:

    venv/Scripts/python.exe -m scripts.simulate_history --reset

To deliberately run it against a remote database (e.g. the deployed one), pass that
database's exact host — a guard against pointing it at the wrong .env by accident:

    python -m scripts.simulate_history --reset --allow-remote-db <DB_HOST>

--reset TRUNCATEs every table (except alembic_version) and re-runs scripts.seed_admin
first. Without it the script refuses to run on a non-empty catalog, so it never mixes
simulated data with leftovers.

How it works:
- Every write goes through the real routes (TestClient → full FastAPI stack: auth,
  validation, services), covering every write endpoint: departments, categories
  (POST/PATCH), products (POST/PATCH/DELETE), cost, prices, stock adjustments, clients
  (POST/PATCH/DELETE), addresses (POST/PATCH/DELETE), orders, pay, cancel, sync/push, login.
- The simulation runs day by day, in chronological order. Sales go through sync/push with
  a backdated occurred_at (90%) or the online POST /orders (10%); price changes go through
  POST .../prices with start_date = the simulated day, so they are already the current
  price for the sales pushed after them.
- The routes stamp created_at/updated_at with the real "now". At the end of each simulated
  day a backfill UPDATE (local DB only) moves those timestamps to the simulated timeline,
  so clients, prices, costs, stock movements and orders all have a coherent history.
- Seller users have no create route, so they are inserted directly (same as seed_admin).

Demand model (so forecasting has real signal to find): upward trend over the year,
weekday and hour-of-day profiles, December peak, summer/winter seasonality per category
(beer/soda up in summer, coffee up in winter), Pareto-like product popularity, quarterly
price increases and fortnightly offers.
"""

import argparse
import random
import sys
import uuid
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.security import hash_password
from app.core.timezone import local_today, local_tz
from app.db.session import SessionLocal, engine
from app.main import app
from app.models.user import Group, GroupPermission, Permission, User, UserCompany, UserGroup
from scripts.seed_admin import ADMIN_PASSWORD, ADMIN_USERNAME, seed

TZ = local_tz()  # business timezone (America/Sao_Paulo by default)
DAYS = 365
SEED = 42

SELLERS = [("ana.vendas", "Ana"), ("bruno.vendas", "Bruno"), ("carla.vendas", "Carla")]
SELLER_PASSWORD = "vendedor123"  # dev-only, same spirit as seed_admin's admin123
SELLER_WEIGHTS = [40, 35, 25]

# department -> category -> [(product name, brand, price)]
CATALOG = {
    "Mercearia": {
        "Grãos e Cereais": [
            ("Arroz Branco Tipo 1 5kg", "Tio João", "27.90"), ("Arroz Integral 1kg", "Camil", "8.49"),
            ("Feijão Carioca 1kg", "Kicaldo", "8.99"), ("Feijão Preto 1kg", "Camil", "9.49"),
            ("Lentilha 500g", "Yoki", "9.90"), ("Grão-de-Bico 500g", "Yoki", "11.90"),
            ("Aveia em Flocos 500g", "Quaker", "7.99"), ("Farinha de Mandioca 1kg", "Yoki", "8.49"),
            ("Açúcar Refinado 1kg", "União", "4.99"),
        ],
        "Massas e Molhos": [
            ("Macarrão Espaguete 500g", "Barilla", "6.49"), ("Macarrão Parafuso 500g", "Renata", "4.99"),
            ("Lasanha 500g", "Barilla", "9.90"), ("Molho de Tomate 340g", "Pomarola", "3.49"),
            ("Extrato de Tomate 190g", "Elefante", "4.29"), ("Molho Pesto 200g", "Bertolli", "15.90"),
            ("Macarrão Instantâneo 85g", "Nissin", "2.29"), ("Queijo Ralado 50g", "Vigor", "5.99"),
        ],
        "Café e Matinais": [
            ("Café Torrado e Moído 500g", "Pilão", "19.90"), ("Café Extraforte 500g", "Melitta", "18.90"),
            ("Café em Cápsulas 10un", "3 Corações", "21.90"), ("Achocolatado 400g", "Nescau", "9.49"),
            ("Cereal Matinal 300g", "Nestlé", "14.90"), ("Biscoito Cream Cracker 200g", "Vitarella", "4.49"),
            ("Biscoito Recheado 130g", "Oreo", "4.99"), ("Torrada Integral 160g", "Bauducco", "6.99"),
            ("Geleia de Morango 230g", "Queensberry", "17.90"),
        ],
        "Óleos e Temperos": [
            ("Óleo de Soja 900ml", "Liza", "7.99"), ("Azeite Extra Virgem 500ml", "Gallo", "34.90"),
            ("Sal Refinado 1kg", "Cisne", "2.99"), ("Vinagre de Maçã 750ml", "Castelo", "5.49"),
            ("Tempero Completo 300g", "Sazón", "6.49"), ("Maionese 500g", "Hellmann's", "11.90"),
            ("Ketchup 400g", "Heinz", "12.90"), ("Mostarda 200g", "Hemmer", "6.99"),
        ],
    },
    "Bebidas": {
        "Refrigerantes": [
            ("Refrigerante Cola 2L", "Coca-Cola", "10.99"), ("Refrigerante Cola Zero 2L", "Coca-Cola", "10.99"),
            ("Refrigerante Guaraná 2L", "Antarctica", "8.49"), ("Refrigerante Laranja 2L", "Fanta", "8.99"),
            ("Refrigerante Limão 2L", "Sprite", "8.99"), ("Refrigerante Cola Lata 350ml", "Coca-Cola", "4.49"),
            ("Refrigerante Guaraná Lata 350ml", "Antarctica", "3.79"),
        ],
        "Sucos e Águas": [
            ("Água Mineral 1,5L", "Crystal", "3.29"), ("Água com Gás 500ml", "São Lourenço", "3.49"),
            ("Suco de Uva Integral 1L", "Aurora", "14.90"), ("Suco de Laranja 1L", "Natural One", "12.90"),
            ("Néctar de Pêssego 1L", "Del Valle", "7.99"), ("Água de Coco 1L", "Kero Coco", "11.90"),
            ("Chá Gelado Limão 1,5L", "Leão", "7.49"),
        ],
        "Cervejas": [
            ("Cerveja Pilsen Lata 350ml", "Skol", "3.49"), ("Cerveja Pilsen Lata 350ml", "Brahma", "3.69"),
            ("Cerveja Puro Malte Lata 350ml", "Heineken", "5.49"), ("Cerveja Long Neck 330ml", "Stella Artois", "6.49"),
            ("Cerveja Pack 12 Latas", "Amstel", "39.90"), ("Cerveja Sem Álcool Lata 350ml", "Heineken 0.0", "5.99"),
            ("Cerveja IPA 500ml", "Colorado", "16.90"),
        ],
    },
    "Laticínios e Frios": {
        "Leites e Derivados": [
            ("Leite Integral 1L", "Italac", "5.49"), ("Leite Desnatado 1L", "Piracanjuba", "5.79"),
            ("Iogurte Natural 170g", "Nestlé", "3.49"), ("Iogurte Grego 100g", "Vigor", "3.99"),
            ("Manteiga com Sal 200g", "Aviação", "14.90"), ("Requeijão Cremoso 200g", "Catupiry", "11.90"),
            ("Creme de Leite 200g", "Nestlé", "3.99"), ("Leite Condensado 395g", "Moça", "7.49"),
            ("Queijo Mussarela Fatiado 150g", "Tirolez", "10.90"),
        ],
        "Frios": [
            ("Presunto Cozido Fatiado 200g", "Sadia", "9.90"), ("Peito de Peru Fatiado 150g", "Seara", "12.90"),
            ("Salame Italiano 100g", "Sadia", "13.90"), ("Mortadela Fatiada 200g", "Perdigão", "6.49"),
            ("Salsicha Hot Dog 500g", "Perdigão", "10.90"), ("Linguiça Toscana 700g", "Seara", "19.90"),
            ("Queijo Prato Fatiado 150g", "Polenghi", "11.49"),
        ],
    },
    "Limpeza": {
        "Limpeza da Casa": [
            ("Detergente Líquido 500ml", "Ypê", "2.79"), ("Desinfetante Lavanda 2L", "Pinho Sol", "12.90"),
            ("Água Sanitária 2L", "Qboa", "7.49"), ("Limpador Multiuso 500ml", "Veja", "6.99"),
            ("Esponja Multiuso 4un", "Scotch-Brite", "7.99"), ("Saco de Lixo 50L 30un", "Dover Roll", "14.90"),
            ("Papel Toalha 2 Rolos", "Snob", "7.49"), ("Lustra Móveis 200ml", "Poliflor", "9.49"),
        ],
        "Lavanderia": [
            ("Sabão em Pó 1,6kg", "Omo", "26.90"), ("Sabão Líquido 3L", "Ariel", "44.90"),
            ("Amaciante 2L", "Comfort", "17.90"), ("Sabão em Barra 5un", "Ypê", "11.90"),
            ("Alvejante Sem Cloro 1L", "Vanish", "16.90"), ("Tira Manchas 450g", "Vanish", "22.90"),
        ],
    },
    "Higiene e Beleza": {
        "Higiene Pessoal": [
            ("Papel Higiênico 12 Rolos", "Neve", "21.90"), ("Sabonete em Barra 85g", "Dove", "3.99"),
            ("Creme Dental 90g", "Colgate", "5.49"), ("Escova Dental Macia", "Oral-B", "8.99"),
            ("Desodorante Aerosol 150ml", "Rexona", "15.90"), ("Fio Dental 50m", "Johnson's", "9.49"),
            ("Absorvente com Abas 8un", "Always", "7.99"), ("Lenço Umedecido 50un", "Huggies", "9.90"),
        ],
        "Cabelos": [
            ("Shampoo 400ml", "Pantene", "21.90"), ("Condicionador 400ml", "Pantene", "23.90"),
            ("Shampoo Anticaspa 200ml", "Clear", "19.90"), ("Creme para Pentear 300ml", "Seda", "13.90"),
            ("Máscara Capilar 300g", "Elseve", "24.90"), ("Gel Fixador 300g", "Bozzano", "12.90"),
        ],
    },
}

# Month factor on the category's demand: summer (Dec–Feb) vs winter (Jun–Aug) products.
SUMMER = {12: 1.45, 1: 1.4, 2: 1.3, 3: 1.1, 6: 0.75, 7: 0.7, 8: 0.8, 11: 1.15}
WINTER = {6: 1.35, 7: 1.4, 8: 1.25, 5: 1.1, 12: 0.85, 1: 0.8, 2: 0.85}
CATEGORY_SEASON = {"Cervejas": SUMMER, "Refrigerantes": SUMMER, "Sucos e Águas": SUMMER, "Café e Matinais": WINTER}

WEEKDAY_FACTOR = [0.85, 0.9, 0.95, 1.0, 1.15, 1.35, 0.8]  # Monday..Sunday
MONTH_FACTOR = {1: 0.9, 2: 0.9, 3: 0.95, 4: 0.95, 5: 1.0, 6: 1.0, 7: 1.0, 8: 1.0, 9: 1.0, 10: 1.05, 11: 1.15, 12: 1.45}
HOURS = list(range(8, 22))
HOUR_WEIGHTS = [3, 4, 6, 8, 10, 9, 6, 5, 6, 8, 10, 9, 6, 3]
PAYMENTS = ["pix", "credit_card", "debit_card", "cash", "other"]
PAYMENT_WEIGHTS = [35, 25, 20, 15, 5]

FIRST_NAMES = (
    "Ana Bruno Carla Daniel Eduarda Felipe Gabriela Henrique Isabela João Juliana Lucas Mariana "
    "Mateus Natália Otávio Patrícia Rafael Sabrina Thiago Vanessa Vinícius Yasmin Gustavo Letícia "
    "Rodrigo Camila Diego Fernanda Leonardo Beatriz André Larissa Marcelo Priscila Ricardo Tatiane "
    "Fábio Aline Caio Bianca Renato Simone Paulo Cláudia Sérgio Débora Wesley Lívia"
).split()
LAST_NAMES = (
    "Silva Santos Oliveira Souza Rodrigues Ferreira Alves Pereira Lima Gomes Costa Ribeiro Martins "
    "Carvalho Almeida Lopes Soares Fernandes Vieira Barbosa Rocha Dias Nascimento Andrade Moreira "
    "Nunes Marques Machado Mendes Freitas Cardoso Ramos Teixeira Correia Pinto"
).split()
STREETS = (
    "Rua das Flores|Avenida Brasil|Rua São Paulo|Rua XV de Novembro|Avenida Paulista|Rua Sete de "
    "Setembro|Rua Tiradentes|Avenida Independência|Rua Barão do Rio Branco|Rua Dom Pedro II|Rua "
    "Marechal Deodoro|Avenida Getúlio Vargas|Rua Duque de Caxias|Rua José Bonifácio|Rua Santos Dumont"
).split("|")
NEIGHBORHOODS = "Centro|Jardim América|Vila Nova|Santa Cruz|Boa Vista|Jardim Paulista|Vila Rica|Bela Vista".split("|")
CITIES = [("Ribeirão Preto", "SP")] * 6 + [("Sertãozinho", "SP"), ("Cravinhos", "SP"), ("Jardinópolis", "SP")]

MONEY = Decimal("0.01")


def money(value) -> Decimal:
    return Decimal(str(value)).quantize(MONEY, rounding=ROUND_HALF_UP)


class Api:
    def __init__(self, client: TestClient, token: str):
        self.client = client
        self.headers = {"Authorization": f"Bearer {token}"}
        self.calls: dict[str, int] = defaultdict(int)

    def call(self, method: str, path: str, expected: int, json=None):
        resp = self.client.request(method, f"/api/v1{path}", json=json, headers=self.headers)
        route = f"{method} " + "/".join("{id}" if p.split("?")[0].isdigit() else p.split("?")[0] for p in path.split("/"))
        self.calls[route] += 1
        if resp.status_code != expected:
            raise RuntimeError(f"{method} {path} -> {resp.status_code} (expected {expected}): {resp.text[:400]}")
        return resp.json() if resp.content else None


def login(client: TestClient, username: str, password: str) -> str:
    resp = client.post("/api/v1/auth/login", data={"username": username, "password": password})
    if resp.status_code != 200:
        raise RuntimeError(f"login {username} -> {resp.status_code}: {resp.text}")
    return resp.json()["access_token"]


# ---------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------


def ensure_allowed_db(allow_remote_db: str | None) -> None:
    host = get_settings().db_host
    if host in ("localhost", "127.0.0.1"):
        return
    if allow_remote_db != host:
        sys.exit(
            f"Refusing to run: DB_HOST is '{host}' (not local). To run against it on purpose, "
            f"pass --allow-remote-db {host}"
        )
    print(f"WARNING: running against REMOTE database {host}")


def reset_database() -> None:
    with engine.begin() as conn:
        tables = conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'alembic_version'")
        ).scalars().all()
        conn.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))
    print(f"Reset: truncated {len(tables)} tables")
    seed()


def create_sellers(filial_id: int) -> None:
    db = SessionLocal()
    try:
        group = Group(name="Vendedor")
        db.add(group)
        db.flush()
        for code in ("orders.manage", "clients.edit"):
            perm = db.execute(select(Permission).where(Permission.code == code)).scalar_one()
            db.add(GroupPermission(group_id=group.id, permission_id=perm.id))
        for username, first_name in SELLERS:
            user = User(
                username=username,
                email=f"{username}@commercia.local",
                password_hash=hash_password(SELLER_PASSWORD),
            )
            db.add(user)
            db.flush()
            db.add(UserGroup(user_id=user.id, group_id=group.id))
            db.add(UserCompany(user_id=user.id, filial_id=filial_id))
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------
# simulation
# ---------------------------------------------------------------------


class Simulation:
    def __init__(self, client: TestClient, admin: Api, sellers: list[Api], filial_id: int, start: date):
        self.rng = random.Random(SEED)
        self.client = client
        self.admin = admin
        self.sellers = sellers
        self.filial_id = filial_id
        self.start = start
        self.products: dict[int, dict] = {}  # id -> name, category, price, offer, cost, stock, weight...
        self.categories: dict[int, str] = {}
        self.clients: list[int] = []
        self.client_weights: dict[int, float] = {}
        self.client_addresses: dict[int, list[int]] = defaultdict(list)
        self.walk_in_client: int | None = None
        self.used_cpfs: set[str] = set()
        self.pending_orders: list[tuple[int, datetime, Api]] = []
        self.offers_until: dict[int, date] = {}
        # timestamps to backfill at the end of each simulated day
        self.client_ts: dict[int, datetime] = {}
        self.order_ts: dict[int, datetime] = {}  # online orders (no occurred_at)
        self.order_updated_ts: dict[int, datetime] = {}
        self.product_updated_ts: dict[int, datetime] = {}
        self.cost_ts: dict[int, datetime] = {}
        self.cancel_ts: dict[int, datetime] = {}
        self.stats: dict[str, int] = defaultdict(int)

    @staticmethod
    def at(day: date, hour: int, minute: int = 0) -> datetime:
        return datetime.combine(day, time(hour, minute), tzinfo=TZ)

    # --- catalog -----------------------------------------------------

    def build_catalog(self, day: date) -> None:
        f = self.filial_id
        ts = self.at(day, 7)
        popularity_rank = []
        for department, categories in CATALOG.items():
            dep_id = self.admin.call("POST", "/catalog/departments", 201, {"name": department})["id"]
            for category, products in categories.items():
                cat = self.admin.call("POST", f"/catalog/categories?filial_id={f}", 201,
                                      {"name": category, "department_id": dep_id})
                self.categories[cat["id"]] = category
                for name, brand, price in products:
                    sku = f"{category[:3].upper()}-{len(self.products) + 1:04d}"
                    barcode = f"789{self.rng.randrange(10**9, 10**10)}"
                    product = self.admin.call("POST", f"/catalog/products?filial_id={f}", 201, {
                        "sku": sku, "name": f"{name} {brand}", "brand": brand, "barcode": barcode,
                        "category_id": cat["id"], "description": f"{name} — {brand}",
                    })
                    price = money(price)
                    cost = money(price * Decimal(str(self.rng.uniform(0.55, 0.75))))
                    self.admin.call("POST", f"/catalog/products/{product['id']}/prices", 201,
                                    {"price_default": str(price), "start_date": day.isoformat()})
                    self.admin.call("PUT", f"/catalog/products/{product['id']}/cost", 200, {"cost_value": str(cost)})
                    self.cost_ts[product["id"]] = ts
                    self.products[product["id"]] = {
                        "name": product["name"], "category": category, "price": price, "offer": Decimal("0"),
                        "cost": cost, "stock": 0, "active": True,
                    }
                    popularity_rank.append(product["id"])

        # Pareto-like popularity: a shuffled rank, weight = 1 / rank^0.9
        self.rng.shuffle(popularity_rank)
        for rank, product_id in enumerate(popularity_rank, start=1):
            self.products[product_id]["weight"] = 1 / rank**0.9
        total_weight = sum(p["weight"] for p in self.products.values())
        for p in self.products.values():
            # expected units/day at ~30 orders/day * ~2.3 items * ~1.6 units
            p["daily_units"] = 30 * 2.3 * 1.6 * p["weight"] / total_weight
            p["target"] = max(15, int(p["daily_units"] * 21) + 5)
            p["reorder"] = max(5, int(p["daily_units"] * 7))

    def restock(self, day: date) -> None:
        for product_id, p in self.products.items():
            if p["active"] and p["stock"] < p["reorder"]:
                delta = p["target"] - p["stock"]
                self.admin.call("POST", f"/catalog/products/{product_id}/stock/adjustments", 201,
                                {"quantity_delta": delta, "reason": "restock"})
                p["stock"] += delta
                self.stats["restocks"] += 1

    def losses_and_returns(self, day: date) -> None:
        for product_id in self.rng.sample(list(self.products), 3):
            p = self.products[product_id]
            if p["active"] and p["stock"] > 3 and self.rng.random() < 0.5:
                lost = self.rng.randint(1, 2)
                self.admin.call("POST", f"/catalog/products/{product_id}/stock/adjustments", 201,
                                {"quantity_delta": -lost, "reason": "loss"})
                p["stock"] -= lost
                self.stats["losses"] += 1
        if day.weekday() == 0:
            product_id = self.rng.choice([pid for pid, p in self.products.items() if p["active"]])
            self.admin.call("POST", f"/catalog/products/{product_id}/stock/adjustments", 201,
                            {"quantity_delta": 1, "reason": "return"})
            self.products[product_id]["stock"] += 1
            self.stats["returns"] += 1
        if day.day == 1:  # monthly inventory count: small correction either way
            product_id = self.rng.choice([pid for pid, p in self.products.items() if p["active"] and p["stock"] > 5])
            delta = self.rng.choice([-2, -1, 1, 2])
            self.admin.call("POST", f"/catalog/products/{product_id}/stock/adjustments", 201,
                            {"quantity_delta": delta, "reason": "adjustment"})
            self.products[product_id]["stock"] += delta

    def set_price(self, product_id: int, day: date, price: Decimal, offer: Decimal) -> None:
        body = {"price_default": str(price), "start_date": day.isoformat()}
        if offer > 0:
            body["price_offer"] = str(offer)
        self.admin.call("POST", f"/catalog/products/{product_id}/prices", 201, body)
        self.products[product_id]["price"] = price
        self.products[product_id]["offer"] = offer
        self.stats["price_changes"] += 1

    def price_events(self, day: date, day_index: int) -> None:
        # offers ending today go back to the regular price
        for product_id, until in list(self.offers_until.items()):
            if until <= day:
                self.set_price(product_id, day, self.products[product_id]["price"], Decimal("0"))
                del self.offers_until[product_id]
        # fortnightly offers: 6 products 10–20% off for a week
        if day_index % 14 == 3:
            candidates = [pid for pid, p in self.products.items() if p["active"] and pid not in self.offers_until]
            for product_id in self.rng.sample(candidates, 6):
                p = self.products[product_id]
                offer = money(p["price"] * Decimal(str(self.rng.uniform(0.80, 0.90))))
                self.set_price(product_id, day, p["price"], offer)
                self.offers_until[product_id] = day + timedelta(days=7)
        # quarterly inflation: ~30% of products +3–8% price, cost follows
        if day_index in (90, 180, 270):
            for product_id, p in self.products.items():
                if p["active"] and product_id not in self.offers_until and self.rng.random() < 0.3:
                    factor = Decimal(str(1 + self.rng.uniform(0.03, 0.08)))
                    self.set_price(product_id, day, money(p["price"] * factor), Decimal("0"))
                    p["cost"] = money(p["cost"] * factor)
                    self.admin.call("PUT", f"/catalog/products/{product_id}/cost", 200, {"cost_value": str(p["cost"])})
                    self.cost_ts[product_id] = self.at(day, 7, 30)

    def catalog_maintenance(self, day: date, day_index: int) -> None:
        f = self.filial_id
        if day_index == 45:  # category renamed
            cat_id = next(cid for cid, name in self.categories.items() if name == "Sucos e Águas")
            self.admin.call("PATCH", f"/catalog/categories/{cat_id}", 200, {"name": "Sucos, Águas e Chás"})
        if day_index in (60, 150, 240):  # product data corrections
            for product_id in self.rng.sample(list(self.products), 3):
                p = self.products[product_id]
                self.admin.call("PATCH", f"/catalog/products/{product_id}", 200,
                                {"description": f"{p['name']} — revisado", "weight": str(round(self.rng.uniform(0.1, 5), 3))})
                self.product_updated_ts[product_id] = self.at(day, 9)
        if day_index in (120, 200):  # a slow seller leaves the mix
            slow = min((pid for pid, p in self.products.items() if p["active"]), key=lambda pid: self.products[pid]["weight"])
            self.admin.call("DELETE", f"/catalog/products/{slow}", 200)
            self.products[slow]["active"] = False
            self.product_updated_ts[slow] = self.at(day, 9)
            self.stats["products_deactivated"] += 1
        if day_index == 210:  # a new launch mid-year
            cat_id = next(cid for cid, name in self.categories.items() if name == "Cervejas")
            product = self.admin.call("POST", f"/catalog/products?filial_id={f}", 201, {
                "sku": "CER-NOVA", "name": "Cerveja Lager Artesanal 473ml Praya", "brand": "Praya",
                "category_id": cat_id,
            })
            price = Decimal("9.90")
            cost = Decimal("6.20")
            self.admin.call("POST", f"/catalog/products/{product['id']}/prices", 201,
                            {"price_default": str(price), "start_date": day.isoformat()})
            self.admin.call("PUT", f"/catalog/products/{product['id']}/cost", 200, {"cost_value": str(cost)})
            self.cost_ts[product["id"]] = self.at(day, 7)
            daily = 1.5
            self.products[product["id"]] = {
                "name": product["name"], "category": "Cervejas", "price": price, "offer": Decimal("0"),
                "cost": cost, "stock": 0, "active": True, "weight": 0.05, "daily_units": daily,
                "target": int(daily * 21) + 5, "reorder": int(daily * 7),
            }

    # --- clients -----------------------------------------------------

    def new_cpf(self) -> str:
        while True:
            cpf = "".join(self.rng.choices("0123456789", k=11))
            if cpf not in self.used_cpfs:
                self.used_cpfs.add(cpf)
                return cpf

    def new_client(self, day: date, walk_in: bool = False) -> int:
        if walk_in:
            first, last = "Consumidor", "Final"
            email = "consumidor.final@commercia.local"
        else:
            first, last = self.rng.choice(FIRST_NAMES), self.rng.choice(LAST_NAMES)
            email = f"{first.lower()}.{last.lower()}.{uuid.uuid4().hex[:6]}@email.com"
        body = {"first_name": first, "last_name": last, "email": email, "cpf": self.new_cpf()}
        if not walk_in:
            body["phone"] = f"169{self.rng.randrange(10**7, 10**8)}"
            if self.rng.random() < 0.7:
                body["birth_date"] = date(self.rng.randint(1955, 2006), self.rng.randint(1, 12), self.rng.randint(1, 28)).isoformat()
        client = self.admin.call("POST", f"/clients?filial_id={self.filial_id}", 201, body)
        client_id = client["id"]
        self.client_ts[client_id] = self.at(day, 7, self.rng.randint(0, 29))  # registered before opening, can buy that day
        if walk_in:
            return client_id

        for _ in range(2 if self.rng.random() < 0.2 else 1):
            city, state = self.rng.choice(CITIES)
            address = self.admin.call("POST", f"/clients/{client_id}/addresses", 201, {
                "street": self.rng.choice(STREETS), "number": str(self.rng.randint(10, 3000)),
                "neighborhood": self.rng.choice(NEIGHBORHOODS), "city": city, "state": state,
                "zip_code": f"14{self.rng.randrange(10**5, 10**6)}",
                **({"complement": f"Apto {self.rng.randint(1, 150)}"} if self.rng.random() < 0.3 else {}),
            })
            self.client_addresses[client_id].append(address["id"])
        self.clients.append(client_id)
        self.client_weights[client_id] = self.rng.lognormvariate(0, 0.9)  # some clients are regulars
        return client_id

    def client_maintenance(self, day: date) -> None:
        if not self.clients:
            return
        if self.rng.random() < 0.3:  # phone changed
            client_id = self.rng.choice(self.clients)
            self.admin.call("PATCH", f"/clients/{client_id}", 200, {"phone": f"169{self.rng.randrange(10**7, 10**8)}"})
        if self.rng.random() < 0.1:  # moved: address updated
            client_id = self.rng.choice(self.clients)
            if self.client_addresses[client_id]:
                address_id = self.client_addresses[client_id][0]
                self.admin.call("PATCH", f"/clients/{client_id}/addresses/{address_id}", 200,
                                {"street": self.rng.choice(STREETS), "number": str(self.rng.randint(10, 3000))})
        if self.rng.random() < 0.05:  # second address removed
            with_two = [cid for cid in self.clients if len(self.client_addresses[cid]) > 1]
            if with_two:
                client_id = self.rng.choice(with_two)
                address_id = self.client_addresses[client_id].pop()
                self.admin.call("DELETE", f"/clients/{client_id}/addresses/{address_id}", 204)
        if self.rng.random() < 0.04 and len(self.clients) > 50:  # client churned: deactivated
            client_id = self.rng.choice(self.clients)
            self.admin.call("DELETE", f"/clients/{client_id}", 200)
            self.clients.remove(client_id)
            self.stats["clients_deactivated"] += 1

    # --- sales -------------------------------------------------------

    def demand_factor(self, day: date, day_index: int) -> float:
        trend = 0.8 + 0.45 * day_index / DAYS  # ~+55% over the year
        return trend * WEEKDAY_FACTOR[day.weekday()] * MONTH_FACTOR[day.month]

    def pick_items(self, day: date) -> list[dict]:
        active = [pid for pid, p in self.products.items() if p["active"]]
        weights = [self.products[pid]["weight"] * CATEGORY_SEASON.get(self.products[pid]["category"], {}).get(day.month, 1.0)
                   for pid in active]
        n_items = self.rng.choices([1, 2, 3, 4, 5], weights=[35, 30, 20, 10, 5])[0]
        chosen: dict[int, int] = {}
        for product_id in self.rng.choices(active, weights=weights, k=n_items):
            qty = self.rng.choices([1, 2, 3, 4], weights=[60, 25, 10, 5])[0]
            if self.products[product_id]["category"] == "Cervejas" and self.rng.random() < 0.4:
                qty += self.rng.choice([2, 4, 6])
            chosen[product_id] = chosen.get(product_id, 0) + qty
        items = []
        for product_id, qty in chosen.items():
            qty = min(qty, self.products[product_id]["stock"])  # out of stock: the client takes what's left
            if qty > 0:
                items.append({"product_id": product_id, "quantity": qty})
        return items

    def sale_body(self, day: date, items: list[dict]) -> dict:
        subtotal = sum(
            (self.products[i["product_id"]]["offer"] or self.products[i["product_id"]]["price"]) * i["quantity"]
            for i in items
        )
        discount = money(subtotal * Decimal(str(self.rng.uniform(0.03, 0.10)))) if self.rng.random() < 0.12 else Decimal("0")
        if self.rng.random() < 0.3 or not self.clients:
            client_id = self.walk_in_client
        else:
            client_id = self.rng.choices(self.clients, weights=[self.client_weights[c] for c in self.clients])[0]
        return {
            "client_id": client_id,
            "payment_method": self.rng.choices(PAYMENTS, weights=PAYMENT_WEIGHTS)[0],
            "discount": str(discount),
            "items": items,
        }

    def sell_day(self, day: date, day_index: int, last_day_index: int) -> None:
        """Replays the day as an event queue in time order — each sale (one sync push per
        sale, or an online POST /orders) and each cancellation hits the API in the same
        order as its simulated timestamp. That keeps stock_movements' id order equal to
        their occurred_at order, so the running stock (resulting_qty) is consistent when
        read chronologically — which Fase 7's daily stock snapshot depends on."""
        n_orders = max(5, int(self.rng.gauss(30 * self.demand_factor(day, day_index), 4)))
        closing = self.at(day, 21, 59)
        recent = day_index > last_day_index - 3  # the last 3 days keep some orders pending
        events: list[tuple[datetime, int, str, object]] = []
        seq = 0
        for _ in range(n_orders):
            moment = self.at(day, self.rng.choices(HOURS, weights=HOUR_WEIGHTS)[0], self.rng.randint(0, 59))
            events.append((moment, seq, "sale", None))
            seq += 1

        while events:
            events.sort()
            moment, _, kind, payload = events.pop(0)
            if kind == "cancel":
                order_id, api = payload
                self.cancel(order_id, moment, api)
                continue

            items = self.pick_items(day)
            if not items:
                self.stats["sales_lost_out_of_stock"] += 1
                continue
            body = self.sale_body(day, items)
            for item in items:
                self.products[item["product_id"]]["stock"] -= item["quantity"]
            api = self.sellers[self.rng.choices(range(len(self.sellers)), weights=SELLER_WEIGHTS)[0]]
            if self.rng.random() < 0.1:  # online sale, straight through POST /orders
                order_id = api.call("POST", f"/orders?filial_id={self.filial_id}", 201, body)["id"]
                self.order_ts[order_id] = moment
                self.stats["orders_online"] += 1
            else:  # offline sale, synced through the mobile push
                body.update(client_reference=str(uuid.UUID(int=self.rng.getrandbits(128))), occurred_at=moment.isoformat())
                result = api.call("POST", f"/sync/push?filial_id={self.filial_id}", 200, {"sales": [body]})["results"][0]
                if result["status"] != "created":
                    raise RuntimeError(f"sync push rejected a sale: {result}")
                order_id = result["order_id"]
                self.stats["orders_synced"] += 1

            # fate of the order: cancelled before paying, paid (sometimes refunded later the
            # same day), or — only in the last days — still pending
            roll = self.rng.random()
            if roll < 0.05:
                cancel_at = max(min(moment + timedelta(minutes=self.rng.randint(5, 90)), closing), moment + timedelta(minutes=5))
                events.append((cancel_at, seq, "cancel", (order_id, api)))
                seq += 1
            elif roll < (0.60 if recent else 1.0):
                api.call("POST", f"/orders/{order_id}/pay", 200)
                self.order_updated_ts[order_id] = moment + timedelta(minutes=self.rng.randint(5, 30))
                if self.rng.random() < 0.01:
                    refund_at = max(min(moment + timedelta(minutes=self.rng.randint(60, 240)), closing), moment + timedelta(minutes=31))
                    events.append((refund_at, seq, "cancel", (order_id, api)))
                    seq += 1
            else:
                self.stats["orders_pending"] += 1

    def cancel(self, order_id: int, ts: datetime, api: Api) -> None:
        order = api.call("POST", f"/orders/{order_id}/cancel", 200)
        for item in order["items"]:
            self.products[item["product_id"]]["stock"] += item["quantity"]
        self.order_updated_ts[order_id] = ts
        self.cancel_ts[order_id] = ts
        self.stats["orders_cancelled"] += 1

    # --- timeline backfill ---------------------------------------------

    def backfill(self, day: date, marks: dict[str, int]) -> None:
        """Moves the real 'now' timestamps the routes just wrote onto the simulated day.
        Rows created in this simulated day are found by id watermark (id > marks[table])."""
        morning, evening = self.at(day, 7, 30), self.at(day, 23, 0)  # before opening / after the last possible cancel (~22:30)
        with engine.begin() as conn:
            conn.execute(text("UPDATE prices SET created_at = :ts WHERE id > :m"), {"ts": morning, "m": marks["prices"]})
            if self.client_ts:
                conn.execute(text("UPDATE clients SET created_at = :ts WHERE id = :id"),
                             [{"ts": ts, "id": cid} for cid, ts in self.client_ts.items()])
            # orders: synced ones were written 1–4 min after the sale (before pay/cancel, >= 5 min); online ones at the sale moment
            conn.execute(text("""
                UPDATE orders SET created_at = occurred_at + make_interval(mins => CAST(1 + id % 4 AS int))
                WHERE id > :m AND occurred_at IS NOT NULL
            """), {"m": marks["orders"]})
            conn.execute(text("UPDATE orders SET updated_at = created_at WHERE id > :m AND occurred_at IS NOT NULL"),
                         {"m": marks["orders"]})
            if self.order_ts:
                conn.execute(text("UPDATE orders SET created_at = :ts, updated_at = :ts WHERE id = :id"),
                             [{"ts": ts, "id": oid} for oid, ts in self.order_ts.items()])
            if self.order_updated_ts:
                conn.execute(text("UPDATE orders SET updated_at = :ts WHERE id = :id"),
                             [{"ts": ts, "id": oid} for oid, ts in self.order_updated_ts.items()])
            conn.execute(text("""
                UPDATE order_items oi SET created_at = o.created_at, updated_at = o.created_at
                FROM orders o WHERE oi.order_id = o.id AND oi.id > :m
            """), {"m": marks["order_items"]})
            # stock movements: sales at the sale moment, cancellations at the cancel moment,
            # manual ones before opening (restock) or after closing (loss/return/count)
            conn.execute(text("""
                UPDATE stock_movements sm SET occurred_at = coalesce(o.occurred_at, o.created_at)
                FROM order_items oi JOIN orders o ON o.id = oi.order_id
                WHERE sm.order_item_id = oi.id AND sm.reason = 'sale' AND sm.id > :m
            """), {"m": marks["stock_movements"]})
            if self.cancel_ts:
                conn.execute(text("""
                    UPDATE stock_movements sm SET occurred_at = :ts
                    FROM order_items oi WHERE sm.order_item_id = oi.id AND oi.order_id = :id
                      AND sm.reason = 'adjustment' AND sm.id > :m
                """), [{"ts": ts, "id": oid, "m": marks["stock_movements"]} for oid, ts in self.cancel_ts.items()])
            conn.execute(text("""
                UPDATE stock_movements SET occurred_at = CASE WHEN reason = 'restock' THEN :morning ELSE :evening END
                WHERE id > :m AND order_item_id IS NULL
            """), {"morning": morning, "evening": evening, "m": marks["stock_movements"]})
            if self.cost_ts:
                conn.execute(text("UPDATE product_costs SET updated_at = :ts WHERE product_id = :id"),
                             [{"ts": ts, "id": pid} for pid, ts in self.cost_ts.items()])
            if self.product_updated_ts:
                conn.execute(text("UPDATE products SET updated_at = :ts WHERE id = :id"),
                             [{"ts": ts, "id": pid} for pid, ts in self.product_updated_ts.items()])
        for mapping in (self.client_ts, self.order_ts, self.order_updated_ts, self.cost_ts, self.product_updated_ts, self.cancel_ts):
            mapping.clear()

    def watermarks(self) -> dict[str, int]:
        with engine.connect() as conn:
            return {
                t: conn.execute(text(f"SELECT coalesce(max(id), 0) FROM {t}")).scalar()
                for t in ("prices", "orders", "order_items", "stock_movements")
            }

    def finish(self, first_day: date) -> None:
        """Timestamps that only make sense once the whole timeline exists."""
        day0 = self.at(first_day, 7)
        with engine.begin() as conn:
            conn.execute(text("UPDATE companies SET created_at = :ts"), {"ts": day0 - timedelta(days=30)})
            conn.execute(text("UPDATE users SET created_at = :ts"), {"ts": day0 - timedelta(days=30)})
            conn.execute(text("UPDATE categories SET created_at = :ts, updated_at = :ts"), {"ts": day0})
            conn.execute(text("UPDATE categories SET updated_at = :ts WHERE name = 'Sucos, Águas e Chás'"),
                         {"ts": self.at(first_day + timedelta(days=45), 9)})
            conn.execute(text("UPDATE products SET created_at = :ts WHERE sku <> 'CER-NOVA'"), {"ts": day0})
            conn.execute(text("UPDATE products SET updated_at = created_at WHERE updated_at > now() - interval '1 day'"))
            conn.execute(text("UPDATE products SET created_at = :ts, updated_at = :ts WHERE sku = 'CER-NOVA'"),
                         {"ts": self.at(first_day + timedelta(days=210), 7)})
            conn.execute(text("""
                UPDATE stocks s SET updated_at = m.last FROM (
                    SELECT product_id, max(occurred_at) AS last FROM stock_movements GROUP BY product_id
                ) m WHERE s.product_id = m.product_id
            """))

    def run(self) -> None:
        last_day_index = DAYS - 1
        first_day = self.start
        for day_index in range(DAYS):
            day = first_day + timedelta(days=day_index)
            marks = self.watermarks()
            if day_index == 0:
                self.build_catalog(day)
                self.walk_in_client = self.new_client(day, walk_in=True)
                for _ in range(120):
                    self.new_client(day)
            else:
                expected_new = 0.6 + 0.6 * day_index / DAYS
                for _ in range(sum(self.rng.random() < expected_new / 2 for _ in range(2))):
                    self.new_client(day)
                self.client_maintenance(day)
            self.catalog_maintenance(day, day_index)
            self.price_events(day, day_index)
            self.restock(day)
            self.sell_day(day, day_index, last_day_index)
            self.losses_and_returns(day)
            self.backfill(day, marks)
            if day_index % 30 == 0 or day_index == last_day_index:
                print(f"  {day.isoformat()}  orders so far: {self.stats['orders_synced'] + self.stats['orders_online']}")
        self.finish(first_day)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--reset", action="store_true", help="TRUNCATE every table and re-seed before simulating")
    parser.add_argument("--allow-remote-db", metavar="DB_HOST", help="run against this exact non-local DB_HOST")
    args = parser.parse_args()

    ensure_allowed_db(args.allow_remote_db)
    if args.reset:
        reset_database()
    with engine.connect() as conn:
        if conn.execute(text("SELECT count(*) FROM products")).scalar():
            sys.exit("Catalog is not empty — run with --reset to start from a clean local database.")
        filial_id = conn.execute(text("SELECT id FROM companies ORDER BY id LIMIT 1")).scalar()
    create_sellers(filial_id)

    client = TestClient(app)
    admin = Api(client, login(client, ADMIN_USERNAME, ADMIN_PASSWORD))
    sellers = [Api(client, login(client, username, SELLER_PASSWORD)) for username, _ in SELLERS]

    start = local_today() - timedelta(days=DAYS)
    print(f"Simulating {DAYS} days: {start} .. {start + timedelta(days=DAYS - 1)} (filial {filial_id})")
    sim = Simulation(client, admin, sellers, filial_id, start)
    sim.run()

    calls: dict[str, int] = defaultdict(int)
    for api in (admin, *sellers):
        for route, n in api.calls.items():
            calls[route] += n
    print("\nRoute calls:")
    for route, n in sorted(calls.items()):
        print(f"  {n:>7}  {route}")
    print("\nStats:")
    for key, n in sorted(sim.stats.items()):
        print(f"  {n:>7}  {key}")


if __name__ == "__main__":
    main()
