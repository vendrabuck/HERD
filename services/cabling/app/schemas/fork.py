import uuid
from datetime import datetime
from typing import Any

from app.schemas._types import OptionalUUIDStr, UUIDStr, UUIDStrList
from app.schemas.topology import InvalidEdge
from pydantic import BaseModel


class ForkCreate(BaseModel):
    """Body for POST /internal/forks (fork-on-activation)."""

    reservation_id: uuid.UUID
    parent_topology_id: uuid.UUID | None = None
    parent_version_id: uuid.UUID | None = None
    created_by: str | None = None


class ForkCreateResponse(BaseModel):
    fork_id: UUIDStr
    version_number: int


class ForkConnectionResponse(BaseModel):
    """One row of the fork's wiring (issue #25 P3a, fork GET)."""

    id: UUIDStr
    device_a_id: UUIDStr
    port_a: str
    device_b_id: UUIDStr
    port_b: str
    layer: str
    physical_connection_id: OptionalUUIDStr = None
    # The canvas edge id this hop was resolved from (issue #345 P3b); NULL when unknown
    # (rows predating the column, or a hop resolved without a known edge id). Additive.
    edge_key: str | None = None
    created_by: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ForkVersionSummary(BaseModel):
    """A fork_versions row without its canvas payload (the version list)."""

    id: UUIDStr
    fork_id: UUIDStr
    version_number: int
    restored_from_id: OptionalUUIDStr = None
    created_at: datetime

    model_config = {"from_attributes": True}


class ForkDetailResponse(BaseModel):
    """GET /internal/forks/{reservation_id}: fork metadata, canvas, wiring, versions."""

    id: UUIDStr
    reservation_id: UUIDStr
    parent_topology_id: OptionalUUIDStr = None
    parent_version_id: OptionalUUIDStr = None
    status: str
    canvas_data: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime
    connections: list[ForkConnectionResponse]
    versions: list[ForkVersionSummary]


class ForkCanvasUpdate(BaseModel):
    """Body for PUT /internal/forks/{reservation_id}/canvas (loose draft edit)."""

    canvas_data: dict[str, Any]


class ForkCanvasUpdateResponse(BaseModel):
    """Loose-edit result: the stored draft's route validation, no reconcile."""

    id: UUIDStr
    valid: bool
    invalid_edges: list[InvalidEdge]


class ForkSaveRequest(BaseModel):
    """Body for POST /internal/forks/{reservation_id}/save (reconcile-on-save)."""

    canvas_data: dict[str, Any]
    created_by: str | None = None


class ForkConnectionDelta(BaseModel):
    """One released or built wire in a save result (issue #25 P3a, ADR 0006).

    Carries the canonical connection identity plus the nullable backing
    physical_connection_id (ADR 0007 Decision 3): reservations relays this shape
    verbatim into the reservation.wiring_changed event so execution can apply the
    recorded hop verbatim (Decision 5). physical_connection_id is additive; a caller
    that only reads the five identity fields is unaffected.
    """

    device_a_id: UUIDStr
    port_a: str
    device_b_id: UUIDStr
    port_b: str
    layer: str
    physical_connection_id: OptionalUUIDStr = None
    # The canvas edge id this hop was resolved from (issue #345 P3b); NULL means
    # ungrouped. Relayed verbatim so the consumer can group the hops of one edge.
    edge_key: str | None = None


class ForkSaveResponse(BaseModel):
    """POST .../save result: the version appended and the release/build delta."""

    fork_id: UUIDStr
    version_number: int
    released: list[ForkConnectionDelta]
    built: list[ForkConnectionDelta]
    unchanged_count: int


class ForkPruneRequest(BaseModel):
    """Body for POST /internal/forks/{reservation_id}/prune-devices (issue #459).

    The device ids removed from the reservation whose fork wiring must release.
    """

    device_ids: list[uuid.UUID]


class ForkPruneResponse(BaseModel):
    """POST .../prune-devices result: the released delta and whether a version landed.

    ``changed`` False means nothing was left to release (an idempotent replay, or the
    devices carried no saved wiring); ``version_number`` is then the current latest
    version, unbumped, and the caller must stage no wiring_changed for it.
    """

    fork_id: UUIDStr
    version_number: int
    changed: bool
    released: list[ForkConnectionDelta]


class ForkArchiveResponse(BaseModel):
    """POST .../archive result: the frozen fork's identity and ARCHIVED status."""

    fork_id: UUIDStr
    reservation_id: UUIDStr
    status: str


class ActiveForkEntry(BaseModel):
    """One ACTIVE fork's reservation_id and its latest fork_version (ADR 0007).

    Added for the P3b sweeper heal (ADR 0007 Decision 2): reservations compares each
    ACTIVE fork's latest fork_version against its wiring ledger to find a save whose
    reservation.wiring_changed staging was missed. Purely additive alongside the
    existing reservation_ids list, which the archive reconciler still reads.
    """

    reservation_id: UUIDStr
    latest_fork_version: int


class ActiveForkListResponse(BaseModel):
    """GET /internal/forks: ACTIVE forks, paginated.

    Two aligned views of the same ORDER-stable page (issue #25 P3a, ADR 0006
    Decision 5; ADR 0007 Decision 2): reservation_ids feeds the standing archive
    reconciler, and forks pairs each of those reservation_ids with its latest
    fork_version for the P3b wiring-staging heal. Both plus the page bounds and the
    unpaginated total.
    """

    reservation_ids: UUIDStrList
    forks: list[ActiveForkEntry]
    total: int
    skip: int
    limit: int
