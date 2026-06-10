import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_serializer


class TopologyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class TopologyClone(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class TopologyUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    canvas_data: dict[str, Any] | None = None
    description: str | None = Field(default=None, max_length=2000)


class TopologyResponse(BaseModel):
    id: uuid.UUID
    name: str
    created_by: uuid.UUID
    owner_name: str = ""
    created_at: datetime
    updated_at: datetime
    modified_by: uuid.UUID | None = None

    model_config = {"from_attributes": True}

    @field_serializer("modified_by")
    def serialize_modified_by(self, value: uuid.UUID | None) -> str | None:
        return str(value) if value else None

    @field_serializer("id", "created_by")
    def serialize_uuid(self, value: uuid.UUID) -> str:
        return str(value)


class TopologyDetail(TopologyResponse):
    canvas_data: dict[str, Any] | None = None


class PaginatedTopologyResponse(BaseModel):
    items: list[TopologyResponse]
    total: int
    skip: int
    limit: int


class TopologyVersionResponse(BaseModel):
    id: uuid.UUID
    topology_id: uuid.UUID
    version_number: int
    name: str
    description: str | None = None
    created_by: uuid.UUID
    author_name: str
    created_at: datetime
    restored_from_id: uuid.UUID | None = None

    model_config = {"from_attributes": True}

    @field_serializer("id", "topology_id", "created_by")
    def _serialize_uuid(self, value: uuid.UUID) -> str:
        return str(value)

    @field_serializer("restored_from_id")
    def _serialize_optional_uuid(self, value: uuid.UUID | None) -> str | None:
        return str(value) if value else None


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
    version_a: uuid.UUID
    version_b: uuid.UUID
    nodes_added: list[dict[str, Any]]
    nodes_removed: list[dict[str, Any]]
    nodes_modified: list[ModifiedItem]
    edges_added: list[dict[str, Any]]
    edges_removed: list[dict[str, Any]]
    edges_modified: list[ModifiedItem]

    @field_serializer("version_a", "version_b")
    def _serialize_uuid(self, value: uuid.UUID) -> str:
        return str(value)


class TopologyRestoreRequest(BaseModel):
    description: str | None = Field(default=None, max_length=2000)
    restore_name: bool = False


class InvalidEdge(BaseModel):
    edge_id: str
    source_device_id: uuid.UUID | None = None
    target_device_id: uuid.UUID | None = None
    layer: str | None = None
    reason: str

    @field_serializer("source_device_id", "target_device_id")
    def _serialize_optional_uuid(self, value: uuid.UUID | None) -> str | None:
        return str(value) if value else None


class TopologyValidationResponse(BaseModel):
    valid: bool
    invalid_edges: list[InvalidEdge]
