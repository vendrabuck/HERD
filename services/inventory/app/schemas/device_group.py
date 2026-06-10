import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class DeviceGroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)


class DeviceGroupUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)


class DeviceGroupDeviceResponse(BaseModel):
    device_id: uuid.UUID
    device_name: str | None = None
    template_name: str | None = None
    added_at: datetime


class DeviceGroupPermissionResponse(BaseModel):
    user_group_id: uuid.UUID
    user_group_name: str | None = None
    assigned_by: uuid.UUID | None = None
    assigned_at: datetime


class DeviceGroupResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    created_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    modified_by: uuid.UUID | None = None
    device_count: int = 0
    user_group_count: int = 0

    model_config = {"from_attributes": True}


class DeviceGroupDetailResponse(DeviceGroupResponse):
    devices: list[DeviceGroupDeviceResponse] = []
    user_groups: list[DeviceGroupPermissionResponse] = []


class DeviceGroupMembershipResponse(BaseModel):
    """Summary of a device group and its user group permissions for a specific device."""

    id: uuid.UUID
    name: str
    description: str | None = None
    user_groups: list[DeviceGroupPermissionResponse] = []


class BulkDeviceIds(BaseModel):
    device_ids: list[uuid.UUID] = Field(max_length=500)


class BulkUserGroupIds(BaseModel):
    user_group_ids: list[uuid.UUID] = Field(max_length=500)


class BulkResult(BaseModel):
    added: int = 0
    skipped: int = 0


class BulkRemoveResult(BaseModel):
    removed: int = 0
    not_found: int = 0


class PaginatedDeviceGroupResponse(BaseModel):
    items: list[DeviceGroupResponse]
    total: int
    skip: int
    limit: int
