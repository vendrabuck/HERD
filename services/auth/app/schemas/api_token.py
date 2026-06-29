import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.user import Role


class CreateApiTokenRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    principal_id: uuid.UUID
    role: Role
    expires_at: datetime | None = None


class ApiTokenCreatedResponse(BaseModel):
    id: uuid.UUID
    name: str
    principal_id: uuid.UUID
    role: Role
    expires_at: datetime | None = None
    # The raw token is returned ONLY here, at creation time, and is never shown
    # again. Store it securely; it cannot be recovered later.
    token: str


class ApiTokenResponse(BaseModel):
    id: uuid.UUID
    name: str
    principal_id: uuid.UUID
    role: Role
    is_active: bool
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class ExchangeTokenRequest(BaseModel):
    token: str = Field(min_length=1)


class ExchangeTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
