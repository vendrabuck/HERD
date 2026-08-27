from datetime import datetime
from typing import Any

from app.schemas._types import OptionalUUIDStr, UUIDStr
from pydantic import BaseModel, Field


class TopologyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class TopologyClone(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class TopologyUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    canvas_data: dict[str, Any] | None = None
    description: str | None = Field(default=None, max_length=2000)


class TopologyResponse(BaseModel):
    id: UUIDStr
    name: str
    created_by: UUIDStr
    owner_name: str = ""
    created_at: datetime
    updated_at: datetime
    modified_by: OptionalUUIDStr = None

    model_config = {"from_attributes": True}


class TopologyDetail(TopologyResponse):
    canvas_data: dict[str, Any] | None = None


class PaginatedTopologyResponse(BaseModel):
    items: list[TopologyResponse]
    total: int
    skip: int
    limit: int


class TopologyVersionResponse(BaseModel):
    id: UUIDStr
    topology_id: UUIDStr
    version_number: int
    name: str
    description: str | None = None
    created_by: UUIDStr
    author_name: str
    created_at: datetime
    restored_from_id: OptionalUUIDStr = None

    model_config = {"from_attributes": True}


class TopologyVersionDetail(TopologyVersionResponse):
    canvas_data: dict[str, Any] | None = None


class PaginatedTopologyVersionResponse(BaseModel):
    items: list[TopologyVersionResponse]
    total: int
    skip: int
    limit: int


class ModifiedItem(BaseModel):
    id: str
    before: dict[str, Any]
    after: dict[str, Any]


class TopologyVersionDiff(BaseModel):
    version_a: UUIDStr
    version_b: UUIDStr
    nodes_added: list[dict[str, Any]]
    nodes_removed: list[dict[str, Any]]
    nodes_modified: list[ModifiedItem]
    edges_added: list[dict[str, Any]]
    edges_removed: list[dict[str, Any]]
    edges_modified: list[ModifiedItem]


class TopologyRestoreRequest(BaseModel):
    description: str | None = Field(default=None, max_length=2000)
    restore_name: bool = False


class InvalidEdge(BaseModel):
    edge_id: str
    source_device_id: OptionalUUIDStr = None
    target_device_id: OptionalUUIDStr = None
    layer: str | None = None
    reason: str


class TopologyValidationResponse(BaseModel):
    valid: bool
    invalid_edges: list[InvalidEdge]
