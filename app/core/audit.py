"""Audit trail writer (RNF11).

Two ways a row reaches `audit_logs`, both inside the transaction of the operation itself
(a rollback drops the log too; a commit always carries it):

1. Automatic — an `after_flush` listener inspects every catalog/client/order object the
   flush created, changed or deleted and logs it, with the changed fields as
   {"field": [old, new]}. No service has to remember to call anything, so a new write
   path can't silently skip the trail.
2. Explicit — `record()`, for what isn't a plain row change: logins (and failed logins),
   user management (one readable row per operation, never the password or its hash).

Who/where: get_current_user stores the authenticated user and the request IP in
`db.info` (one Session per request), and the listener reads them from there. Reads (GET)
are deliberately not logged — decided 2026-11-09.
"""

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from fastapi import Request
from sqlalchemy import event, insert, inspect
from sqlalchemy.orm import Session

from app.models.audit import AuditLog
from app.models.catalog import Category, Department, Price, Product, ProductCost
from app.models.enums import OrderStatus, RecordStatus
from app.models.sales import Client, ClientAddress, Order, StockMovement
from app.models.user import User

_ACTOR = "audit_actor"
_IP = "audit_ip"
_SOURCE = "audit_source"

# model -> entity_type. Stock rows are covered by their StockMovement; order items by the
# order; prices/costs/stock adjustments are logged as their own entities.
_AUDITED: dict[type, str] = {
    Department: "department",
    Category: "category",
    Product: "product",
    Price: "price",
    ProductCost: "cost",
    StockMovement: "stock",
    Client: "client",
    ClientAddress: "client_address",
    Order: "order",
}
_SKIP_FIELDS = {"id", "created_at", "updated_at", "created_by_id"}


def client_ip(request: Request) -> str | None:
    # Behind Caddy the socket peer is the proxy; the real client is X-Forwarded-For's first hop.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return request.client.host[:64] if request.client else None


def bind_request(db: Session, user: User | None, request: Request | None) -> None:
    db.info[_ACTOR] = user
    if request is not None:
        db.info[_IP] = client_ip(request)


def set_source(db: Session, source: str) -> None:
    """e.g. "sync": tags the rows logged for this request (offline sale vs online)."""
    db.info[_SOURCE] = source


def _json(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, UUID):
        return str(value)
    return value


def _row(db: Session, action: str, entity_type: str | None, entity_id: int | None,
         filial_id: int | None, details: dict[str, Any] | None, user: User | None = None) -> dict[str, Any]:
    actor = user if user is not None else db.info.get(_ACTOR)
    details = {k: _json(v) for k, v in (details or {}).items()}
    if db.info.get(_SOURCE):
        details.setdefault("source", db.info[_SOURCE])
    return {
        "user_id": actor.id if actor is not None else None,
        "username": actor.username if actor is not None else None,
        "filial_id": filial_id,
        "action": action,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "ip": db.info.get(_IP),
        "details": details,
    }


def record(db: Session, action: str, *, entity_type: str | None = None, entity_id: int | None = None,
           filial_id: int | None = None, details: dict[str, Any] | None = None,
           user: User | None = None) -> None:
    """Adds an explicit log row to the current transaction (committed with the operation)."""
    db.add(AuditLog(**_row(db, action, entity_type, entity_id, filial_id, details, user)))


# ---------------------------------------------------------------------
# automatic logging of row changes
# ---------------------------------------------------------------------


def _columns(obj) -> list[str]:
    return [attr.key for attr in inspect(obj).mapper.column_attrs if attr.key not in _SKIP_FIELDS]


def _snapshot(obj) -> dict[str, Any]:
    return {key: getattr(obj, key) for key in _columns(obj)}


def _changes(obj) -> dict[str, list[Any]]:
    state = inspect(obj)
    changes = {}
    for key in _columns(obj):
        history = state.attrs[key].history
        if history.has_changes():
            old = history.deleted[0] if history.deleted else None
            new = history.added[0] if history.added else None
            if old != new:
                changes[key] = [_json(old), _json(new)]
    return changes


def _filial_of(session: Session, obj) -> int | None:
    for attr in ("filial_id", "company_id"):
        value = getattr(obj, attr, None)
        if value is not None:
            return value
    with session.no_autoflush:
        if isinstance(obj, ClientAddress):
            parent = session.get(Client, obj.client_id)
            return parent.filial_id if parent is not None else None
        if isinstance(obj, ProductCost):
            parent = session.get(Product, obj.product_id)
            return parent.filial_id if parent is not None else None
    return None


def _update_action(entity: str, obj, changes: dict[str, list[Any]]) -> str:
    new_status = changes.get("status", [None, None])[1]
    if isinstance(obj, Order):
        if new_status == OrderStatus.PAID.value:
            return "order.pay"
        if new_status == OrderStatus.CANCELLED.value:
            return "order.cancel"
    if new_status == RecordStatus.INACTIVE.value:
        return f"{entity}.deactivate"
    return f"{entity}.update"


def _create_details(obj) -> dict[str, Any]:
    if isinstance(obj, Order):
        return {k: getattr(obj, k) for k in
                ("client_id", "total", "discount", "payment_method", "client_reference", "occurred_at")}
    return _snapshot(obj)


@event.listens_for(Session, "after_flush")
def _log_flushed_changes(session: Session, _flush_context) -> None:
    rows = []
    for obj in session.new:
        entity = _AUDITED.get(type(obj))
        if entity is None:
            continue
        if isinstance(obj, StockMovement):
            if obj.order_item_id is not None:  # a sale or its cancellation — logged as the order
                continue
            action = "stock.adjust"
        elif isinstance(obj, (Price, ProductCost)):
            action = f"{entity}.set"
        else:
            action = f"{entity}.create"
        rows.append(_row(session, action, entity, obj.id, _filial_of(session, obj), _create_details(obj)))

    for obj in session.dirty:
        entity = _AUDITED.get(type(obj))
        if entity is None or isinstance(obj, StockMovement):
            continue
        changes = _changes(obj)
        if not changes:
            continue
        action = "cost.set" if isinstance(obj, ProductCost) else _update_action(entity, obj, changes)
        rows.append(_row(session, action, entity, obj.id, _filial_of(session, obj), changes))

    for obj in session.deleted:
        entity = _AUDITED.get(type(obj))
        if entity is None:
            continue
        rows.append(_row(session, f"{entity}.delete", entity, obj.id, _filial_of(session, obj), _snapshot(obj)))

    if rows:
        # Core insert on the flush's own connection: same transaction, and it doesn't add
        # objects to the session mid-flush.
        session.connection().execute(insert(AuditLog.__table__), rows)
