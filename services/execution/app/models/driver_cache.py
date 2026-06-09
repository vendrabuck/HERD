import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.config import settings
from app.database import Base

_schema = settings.db_schema or None


class DriverCache(Base):
    __tablename__ = "driver_cache"
    __table_args__ = {"schema": _schema} if _schema else {}

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    driver_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, unique=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    local_path: Mapped[str] = mapped_column(String(512), nullable=False)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Driver-published config schema (the JSON-encoded dict returned by
    # Driver.config_schema()), captured once per SHA256 at first load. NULL
    # means the driver did not publish a schema (or extraction failed and we
    # degrade to the registry). See issue #23.
    config_schema_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    cached_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
