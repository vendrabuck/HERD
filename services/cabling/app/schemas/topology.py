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
    """One canvas edge `_run_topology_validation` could not accept, with why.

    ``reason`` is a plain str (no enum), currently one of:

    - ``missing_device``: an endpoint's node id resolves to neither a known device nor
      a network element.
    - ``no_path``: both endpoints are known devices, but the cabling graph has no
      physical path between them.
    - ``element_to_element`` (ADR 0012 phase 1, issue #22): both endpoints are network
      element nodes. Two elements have no device and no port between them.
    - ``element_edge_no_port`` (ADR 0012 phase 1): one endpoint is a network element
      and the other a known device, but the device-side port name
      (``source_port_name``/``target_port_name``, whichever names the device) is
      missing or empty.

    A device-to-element edge with a non-empty device-side port name is VALID and never
    appears here.
    """

    edge_id: str
    source_device_id: OptionalUUIDStr = None
    target_device_id: OptionalUUIDStr = None
    layer: str | None = None
    reason: str


class TopologyValidationResponse(BaseModel):
    valid: bool
    invalid_edges: list[InvalidEdge]
