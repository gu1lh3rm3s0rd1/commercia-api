from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class CompanyRead(ORMModel):
    id: int
    filial_code: str
    corporate_name: str
    fantasy_name: str | None
    cnpj: str
    created_at: datetime
