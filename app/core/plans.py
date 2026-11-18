"""Subscription plans (Fase 12, migration 0018) and what each one unlocks.

From the TCC's Figura 16: Pro and Hulk both have the hybrid sales app, the dashboards and the
"análise inteligente" (analytics and forecasts); only Hulk has the "Assistente IA" — the chat
(RF08) and the agent (RF09). Support channel and development requests are services, not
features of the system. Changing what a plan unlocks = editing PLAN_FEATURES (no migration).

The plan is per store: a user reaches a feature only in the stores whose plan has it.
"""

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.deps import get_accessible_filial_ids
from app.db.session import get_db
from app.models.company import Company
from app.models.enums import SubscriptionPlan

AI_ASSISTANT = "ai_assistant"  # chat (RF08) + agent (RF09)

PLAN_FEATURES: dict[SubscriptionPlan, frozenset[str]] = {
    SubscriptionPlan.PRO: frozenset(),
    SubscriptionPlan.HULK: frozenset({AI_ASSISTANT}),
}
FEATURE_LABELS = {AI_ASSISTANT: "Assistente IA"}


def required_plan(feature: str) -> SubscriptionPlan:
    return next(plan for plan, features in PLAN_FEATURES.items() if feature in features)


class PlanRequired(Exception):
    """A feature the user's stores' plans don't include → 403 "Plano insuficiente" (UC012,
    alternative flow a), with the plan that would unlock it, for the panel's upgrade message."""

    def __init__(self, feature: str):
        super().__init__(feature)
        self.feature = feature
        self.plan = required_plan(feature)


def stores_with(db: Session, filial_ids: set[int], feature: str) -> set[int]:
    plans = [plan for plan, features in PLAN_FEATURES.items() if feature in features]
    if not filial_ids or not plans:
        return set()
    return set(db.execute(select(Company.id).where(Company.id.in_(filial_ids), Company.plan.in_(plans))).scalars().all())


def require_feature(feature: str):
    """Dependency factory: the subset of the user's stores that have `feature`; PlanRequired
    when none does. Depends(require_feature(AI_ASSISTANT))."""

    def _checker(db: Session = Depends(get_db), accessible: set[int] = Depends(get_accessible_filial_ids)) -> set[int]:
        stores = stores_with(db, accessible, feature)
        if not stores:
            raise PlanRequired(feature)
        return stores

    return _checker
