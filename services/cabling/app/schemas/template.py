import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, field_serializer


class TemplateCreate(BaseModel):
    name: str
    description: str | None = None
    canvas_data: dict[str, Any] | None = None


class TemplateFromTopologyRequest(BaseModel):
    name: str
    description: str | None = None


class TemplateUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    canvas_data: dict[str, Any] | None = None


class TemplateResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None = None
    created_by: uuid.UUID
    owner_name: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}

    @field_serializer("id", "created_by")
    def _ser_uuid(self, value: uuid.UUID) -> str:
        return str(value)


class TemplateDetail(TemplateResponse):
    canvas_data: dict[str, Any] | None = None


class PaginatedTemplateResponse(BaseModel):
    items: list[TemplateResponse]
    total: int
    skip: int
    limit: int


class InstantiateRequest(BaseModel):
    name: str
    role_assignments: dict[str, uuid.UUID]
