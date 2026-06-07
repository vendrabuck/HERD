import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.config import settings
from app.database import Base

_schema = settings.db_schema or None


class Topology(Base):
    __tablename__ = "topologies"
    __table_args__ = {"schema": _schema} if _schema else {}

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    owner_name: Mapped[str] = mapped_column(
        String(150), nullable=True, default="", server_default=""
    )
    canvas_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    modified_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


_topology_fk = f"{_schema}.topologies.id" if _schema else "topologies.id"
_version_fk = f"{_schema}.topology_versions.id" if _schema else "topology_versions.id"


class TopologyVersion(Base):
    __tablename__ = "topology_versions"
    # Mirrors migration 0005's uq_topology_versions_topology_version. Declaring it
    # on the model keeps model-built schemas (e.g. the SQLite test DB) in sync with
    # the migration so the version-number uniqueness invariant is enforced there too.
    __table_args__ = (
        UniqueConstraint(
            "topology_id", "version_number", name="uq_topology_versions_topology_version"
        ),
        *(({"schema": _schema},) if _schema else ()),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    topology_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(_topology_fk, ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    canvas_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    author_name: Mapped[str] = mapped_column(String(150), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    restored_from_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(_version_fk, ondelete="SET NULL"),
        nullable=True,
    )
