from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

# The closed v1 element vocabulary (ADR 0012 "Canvas shape"), mirrored from
# the frontend's NetworkElementType (frontend/src/types/topology.types.ts).
# Kept in sync by hand: the AI path is the only backend producer of this
# value, so a fifth element type needs a matching frontend/ADR change too.
NetworkElementType = Literal["vlan_segment", "subnet", "external_cloud", "patch_trunk"]


class GenerateRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=20000)


class ExtractedFile(BaseModel):
    filename: str
    text: str
    truncated: bool = False


class ProposedDevice(BaseModel):
    role: str
    template_name: str
    topology_type: str = "PHYSICAL"
    device: dict[str, Any] | None = None
    config: dict[str, Any] | None = None


class ProposedEdge(BaseModel):
    source_role: str
    target_role: str
    layer: str = "L2"


# A proposed network element (issue #632): role is a synthetic identifier the
# model invents, distinct from a device role but drawn from the same
# namespace (D1: roles are unique across devices AND elements). attrs is
# descriptive only in v1 (ADR 0012), so it is not validated beyond being a
# JSON object here; the tool schema allowlists its keys at the LLM boundary.
class ProposedElement(BaseModel):
    role: str
    element_type: NetworkElementType
    label: str
    attrs: dict[str, Any] = Field(default_factory=dict)


class GenerateResponse(BaseModel):
    purpose: str
    devices: list[ProposedDevice]
    edges: list[ProposedEdge]
    # Additive and optional (D1): a device-only proposal from before this
    # field existed still validates unchanged.
    elements: list[ProposedElement] = Field(default_factory=list)
    notes: str = ""
    file_summaries: list[dict[str, Any]] = Field(default_factory=list)


class CommitDevice(BaseModel):
    role: str
    device_id: str
    position: dict[str, float] | None = None
    config: dict[str, Any] | None = None
    connection_type: str | None = None


class CommitEdge(BaseModel):
    source_role: str
    target_role: str
    layer: str = "L2"


# Mirrors ProposedElement for the accept/commit side of the flow (D1).
class CommitElement(BaseModel):
    role: str
    element_type: NetworkElementType
    label: str
    attrs: dict[str, Any] = Field(default_factory=dict)


class CommitRequest(BaseModel):
    topology_name: str = Field(..., min_length=1, max_length=200)
    purpose: str | None = Field(default=None, max_length=500)
    start_time: datetime
    end_time: datetime
    devices: list[CommitDevice]
    edges: list[CommitEdge] = Field(default_factory=list)
    # Additive and optional (D1): existing device-only commit requests still
    # validate unchanged.
    elements: list[CommitElement] = Field(default_factory=list)
    apply_configs: bool = False

    @field_validator("end_time")
    @classmethod
    def end_after_start(cls, v: datetime, info: Any) -> datetime:
        start = info.data.get("start_time")
        if start and v <= start:
            raise ValueError("end_time must be after start_time")
        return v

    @field_validator("devices")
    @classmethod
    def devices_not_empty(cls, v: list[CommitDevice]) -> list[CommitDevice]:
        if not v:
            raise ValueError("At least one device is required")
        return v


class DeviceConfigResult(BaseModel):
    role: str
    device_id: str
    status: str
    error: str | None = None
    run_id: str | None = None


class CommitResponse(BaseModel):
    topology_id: str
    reservation_id: str
    config_results: list[DeviceConfigResult] = Field(default_factory=list)
