"""The permissions the API checks and the fixed access profiles (groups) built from them.

Profiles are FIXED (decided 2026-11-09): created by migration 0010 / scripts/seed_admin.py,
no route creates or edits them. `groups` has no filial column — it's shared by every
store — so letting one store's admin edit "Gerente" would change it for all stores.
Store admins choose which profile(s) each of their users gets.

Migration 0010 repeats these values as literal SQL (migrations must not import app code
that may change later); keep both in sync if a permission is ever added.
"""

PERMISSIONS: dict[str, str] = {
    "users.manage": "Gerenciar usuários",
    "products.edit": "Gerenciar catálogo (categorias/produtos/preços/estoque)",
    "clients.edit": "Gerenciar clientes",
    "orders.manage": "Gerenciar vendas",
    "audit.view": "Consultar log de auditoria",
    "purchases.manage": "Gerenciar compras e fornecedores",
}

GROUPS: dict[str, tuple[str, ...]] = {
    "Administrador": ("users.manage", "products.edit", "clients.edit", "orders.manage", "audit.view",
                      "purchases.manage"),
    "Gerente": ("products.edit", "clients.edit", "orders.manage", "purchases.manage"),
    "Vendedor": ("orders.manage", "clients.edit"),
}
