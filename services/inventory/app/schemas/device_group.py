import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.column_bounds import column_string

# Matches the device_groups.name column width (String(100), app/models/device_group.py).
DEVICE_GROUP_NAME_MAX_LENGTH = 100
DeviceGroupName = column_string(DEVICE_GROUP_NAME_MAX_LENGTH, min_length=1)
# The description column is Text; 2000 characters is the product's own bound
# (INV-GRP-2).
DeviceGroupDescription = column_string(2000)


class DeviceGroupCreate(BaseModel):
    name: DeviceGroupName
    description: DeviceGroupDescription | None = None


class DeviceGroupUpdate(BaseModel):
    name: DeviceGroupName | None = None
    description: DeviceGroupDescription | None = None


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
