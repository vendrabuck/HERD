from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


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


class GenerateResponse(BaseModel):
    purpose: str
    devices: list[ProposedDevice]
    edges: list[ProposedEdge]
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


class CommitRequest(BaseModel):
    topology_name: str = Field(..., min_length=1, max_length=200)
    purpose: str | None = Field(default=None, max_length=500)
    start_time: datetime
    end_time: datetime
    devices: list[CommitDevice]
    edges: list[CommitEdge] = Field(default_factory=list)
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
