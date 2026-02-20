import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from app.models.device import DeviceStatus, DeviceType, TopologyType


class DeviceCreate(BaseModel):
    name: str
    device_type: DeviceType
    topology_type: TopologyType
    status: DeviceStatus = DeviceStatus.AVAILABLE
    location: str | None = None
    specs: dict[str, Any] | None = None
    description: str | None = None


class DeviceUpdate(BaseModel):
    name: str | None = None
    device_type: DeviceType | None = None
    topology_type: TopologyType | None = None
    status: DeviceStatus | None = None
    location: str | None = None
    specs: dict[str, Any] | None = None
    description: str | None = None


class DeviceResponse(BaseModel):
    id: uuid.UUID
    name: str
    device_type: DeviceType
    topology_type: TopologyType
    status: DeviceStatus
    location: str | None
    specs: dict[str, Any] | None
    description: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
