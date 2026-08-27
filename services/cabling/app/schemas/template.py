import uuid
from datetime import datetime
from typing import Any

from app.schemas._types import UUIDStr
from pydantic import BaseModel


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
    id: UUIDStr
    name: str
    description: str | None = None
    created_by: UUIDStr
    owner_name: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


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
