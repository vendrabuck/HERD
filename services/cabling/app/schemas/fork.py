import uuid
from datetime import datetime
from typing import Any

from app.schemas.topology import InvalidEdge
from pydantic import BaseModel, field_serializer


class ForkCreate(BaseModel):
    """Body for POST /internal/forks (fork-on-activation)."""

    reservation_id: uuid.UUID
    parent_topology_id: uuid.UUID | None = None
    parent_version_id: uuid.UUID | None = None
    created_by: str | None = None


class ForkCreateResponse(BaseModel):
    fork_id: uuid.UUID
    version_number: int

    @field_serializer("fork_id")
    def serialize_fork_id(self, value: uuid.UUID) -> str:
        return str(value)


class ForkConnectionResponse(BaseModel):
    """One row of the fork's wiring (issue #25 P3a, fork GET)."""

    id: uuid.UUID
    device_a_id: uuid.UUID
    port_a: str
    device_b_id: uuid.UUID
    port_b: str
    layer: str
    physical_connection_id: uuid.UUID | None = None
    created_by: str
    created_at: datetime

    model_config = {"from_attributes": True}

    @field_serializer("id", "device_a_id", "device_b_id")
    def _serialize_uuid(self, value: uuid.UUID) -> str:
        return str(value)

    @field_serializer("physical_connection_id")
    def _serialize_optional_uuid(self, value: uuid.UUID | None) -> str | None:
        return str(value) if value else None


class ForkVersionSummary(BaseModel):
    """A fork_versions row without its canvas payload (the version list)."""

    id: uuid.UUID
    fork_id: uuid.UUID
    version_number: int
    restored_from_id: uuid.UUID | None = None
    created_at: datetime

    model_config = {"from_attributes": True}

    @field_serializer("id", "fork_id")
    def _serialize_uuid(self, value: uuid.UUID) -> str:
        return str(value)

    @field_serializer("restored_from_id")
    def _serialize_optional_uuid(self, value: uuid.UUID | None) -> str | None:
        return str(value) if value else None


class ForkDetailResponse(BaseModel):
    """GET /internal/forks/{reservation_id}: fork metadata, canvas, wiring, versions."""

    id: uuid.UUID
    reservation_id: uuid.UUID
    parent_topology_id: uuid.UUID | None = None
    parent_version_id: uuid.UUID | None = None
    status: str
    canvas_data: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime
    connections: list[ForkConnectionResponse]
    versions: list[ForkVersionSummary]

    @field_serializer("id", "reservation_id")
    def _serialize_uuid(self, value: uuid.UUID) -> str:
        return str(value)

    @field_serializer("parent_topology_id", "parent_version_id")
    def _serialize_optional_uuid(self, value: uuid.UUID | None) -> str | None:
        return str(value) if value else None


class ForkCanvasUpdate(BaseModel):
    """Body for PUT /internal/forks/{reservation_id}/canvas (loose draft edit)."""

    canvas_data: dict[str, Any]


class ForkCanvasUpdateResponse(BaseModel):
    """Loose-edit result: the stored draft's route validation, no reconcile."""

    id: uuid.UUID
    valid: bool
    invalid_edges: list[InvalidEdge]

    @field_serializer("id")
    def _serialize_uuid(self, value: uuid.UUID) -> str:
        return str(value)


class ForkSaveRequest(BaseModel):
    """Body for POST /internal/forks/{reservation_id}/save (reconcile-on-save)."""

    canvas_data: dict[str, Any]
    created_by: str | None = None


class ForkConnectionDelta(BaseModel):
    """One released or built wire in a save result (issue #25 P3a, ADR 0006)."""

    device_a_id: uuid.UUID
    port_a: str
    device_b_id: uuid.UUID
    port_b: str
    layer: str

    @field_serializer("device_a_id", "device_b_id")
    def _serialize_uuid(self, value: uuid.UUID) -> str:
        return str(value)


class ForkSaveResponse(BaseModel):
    """POST .../save result: the version appended and the release/build delta."""

    fork_id: uuid.UUID
    version_number: int
    released: list[ForkConnectionDelta]
    built: list[ForkConnectionDelta]
    unchanged_count: int

    @field_serializer("fork_id")
    def _serialize_uuid(self, value: uuid.UUID) -> str:
        return str(value)


class ForkArchiveResponse(BaseModel):
    """POST .../archive result: the frozen fork's identity and ARCHIVED status."""

    fork_id: uuid.UUID
    reservation_id: uuid.UUID
    status: str

    @field_serializer("fork_id", "reservation_id")
    def _serialize_uuid(self, value: uuid.UUID) -> str:
        return str(value)
