import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SecretType = Literal["api_key", "ssh_key", "password", "token", "generic"]


class SecretCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    type: SecretType = "generic"
    description: str | None = Field(default=None, max_length=1024)
    data: dict[str, str] = Field(min_length=1)


class SecretUpdate(BaseModel):
    type: SecretType | None = None
    description: str | None = Field(default=None, max_length=1024)
    # When present, the payload is re-encrypted wholesale; there is no partial
    # field merge (the plaintext is one blob, ADR 0003 decision point 2).
    data: dict[str, str] | None = Field(default=None, min_length=1)


class SecretResponse(BaseModel):
    """Metadata only; plaintext is never carried on this shape."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    type: str
    description: str | None
    key_version: int
    created_by: uuid.UUID | None
    updated_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class SecretValueResponse(BaseModel):
    id: uuid.UUID
    name: str
    data: dict[str, str]


class RotateResponse(BaseModel):
    new_version: int
    reencrypted: int
