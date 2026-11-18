from datetime import datetime

from pydantic import BaseModel, ConfigDict, computed_field

from app.core.plans import PLAN_FEATURES
from app.models.enums import SubscriptionPlan


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class CompanyRead(ORMModel):
    id: int
    filial_code: str
    corporate_name: str
    fantasy_name: str | None
    cnpj: str
    created_at: datetime
    plan: SubscriptionPlan  # added with migration 0018

    @computed_field
    @property
    def features(self) -> list[str]:
        """What the plan unlocks (e.g. ["ai_assistant"]) — the panel shows or hides the AI screens by it."""
        return sorted(PLAN_FEATURES[self.plan])


class PlanUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan: SubscriptionPlan
