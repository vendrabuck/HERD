import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class GroupCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)


class GroupUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)


class GroupMemberResponse(BaseModel):
    user_id: uuid.UUID
    username: str
    email: str
    added_at: datetime

    model_config = {"from_attributes": True}


class GroupResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    created_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    modified_by: uuid.UUID | None = None

    model_config = {"from_attributes": True}


class GroupDetailResponse(GroupResponse):
    members: list[GroupMemberResponse] = []


class AddMemberRequest(BaseModel):
    user_id: uuid.UUID


class BulkAddMembersRequest(BaseModel):
    user_ids: list[uuid.UUID] = Field(max_length=500)


class BulkRemoveMembersRequest(BaseModel):
    user_ids: list[uuid.UUID] = Field(max_length=500)


class BulkUserGroupsRequest(BaseModel):
    user_ids: list[uuid.UUID] = Field(max_length=500)


class BulkMemberAddResponse(BaseModel):
    added: int = 0
    skipped: int = 0


class BulkMemberRemoveResponse(BaseModel):
    removed: int = 0
    not_found: int = 0


class PaginatedGroupResponse(BaseModel):
    items: list[GroupResponse]
    total: int
    skip: int
    limit: int
