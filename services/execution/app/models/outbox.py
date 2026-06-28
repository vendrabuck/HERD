"""Transactional outbox table for the execution service (issue #21).

Concrete model built from the shared `OutboxMixin` against the execution
service's own `Base`, so the table lives in the execution schema. The health
scheduler enqueues a row in the same transaction as the device health-status
update; the relay (started in main.py) drains unpublished rows to JetStream.
"""

from herd_common.outbox import OutboxMixin

from app.config import settings
from app.database import Base

_schema = settings.db_schema or None


class OutboxEvent(OutboxMixin, Base):
    __tablename__ = "outbox"
    __table_args__ = {"schema": _schema} if _schema else {}
