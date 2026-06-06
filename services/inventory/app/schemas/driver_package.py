import uuid
from datetime import datetime

from pydantic import BaseModel


class DriverPackageInfo(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None = None
    connection_type: str
    filename: str
    size_bytes: int
    sha256: str
    uploaded_by: str
    supports_dry_run: bool = False
    created_at: datetime
    updated_at: datetime
    modified_by: uuid.UUID | None = None

    model_config = {"from_attributes": True}


class DriverUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    connection_type: str | None = None


class PaginatedDriverResponse(BaseModel):
    items: list[DriverPackageInfo]
    total: int
    skip: int
    limit: int
