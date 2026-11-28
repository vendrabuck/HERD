import uuid
from datetime import datetime
from typing import Any

from app.schemas._types import NAME_MAX_LENGTH, ColumnText, UUIDStr
from app.services.canvas_shape import CanvasData
from pydantic import BaseModel, Field


# Bounds match the topology schemas (app/schemas/topology.py), issue #1005.
class TemplateCreate(BaseModel):
    name: ColumnText = Field(min_length=1, max_length=NAME_MAX_LENGTH)
    description: ColumnText | None = Field(default=None, max_length=2000)
    canvas_data: CanvasData | None = None


class TemplateFromTopologyRequest(BaseModel):
    name: ColumnText = Field(min_length=1, max_length=NAME_MAX_LENGTH)
    description: ColumnText | None = Field(default=None, max_length=2000)


class TemplateUpdate(BaseModel):
    name: ColumnText | None = Field(default=None, min_length=1, max_length=NAME_MAX_LENGTH)
    description: ColumnText | None = Field(default=None, max_length=2000)
    canvas_data: CanvasData | None = None


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
    # Names the new topology, so it takes the topology name bound.
    name: ColumnText = Field(min_length=1, max_length=NAME_MAX_LENGTH)
    role_assignments: dict[str, uuid.UUID]
