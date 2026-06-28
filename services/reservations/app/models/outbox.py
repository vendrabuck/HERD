"""Transactional outbox table for the reservations service (issue #21).

Concrete model bound to this service's Base so the `outbox` table lands in the
reservations schema. The column shape and the enqueue/relay/prune helpers live
in `herd_common.outbox`; this module only names the table. Events are staged via
`enqueue_event` in the same transaction as the state change they describe, and a
background relay (`run_outbox_relay`, wired in `app.main`) publishes them to
JetStream at-least-once.
"""

from herd_common.outbox import OutboxMixin

from app.config import settings
from app.database import Base

_schema = settings.db_schema or None


class OutboxEvent(OutboxMixin, Base):
    __tablename__ = "outbox"
    # The reservations models carry their schema per-table via __table_args__
    # (the Base has no schema-scoped MetaData), so the outbox must do the same or
    # it lands in the default `public` schema instead of `reservations`.
    __table_args__ = {"schema": _schema} if _schema else {}
