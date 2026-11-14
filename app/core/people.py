"""Roles a person (app/models/sales.py::Person) can have in a store. Several at once are
allowed. Adding a role = adding it here — the column is a plain text[] (decided 2026-11-14:
"the people table receives every registration; clients are people filtered by type")."""

CLIENT = "client"
SUPPLIER = "supplier"

PERSON_ROLES: dict[str, str] = {
    CLIENT: "Cliente",
    SUPPLIER: "Fornecedor",
}
