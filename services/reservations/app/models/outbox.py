"""Transactional outbox table for the reservations service (issue #21).

Concrete model bound to this service's Base so the `outbox` table lands in the
reservations schema. The column shape and the enqueue/relay/prune helpers live
in `herd_common.outbox`; this module only names the table. Events are staged via
`enqueue_event` in the same transaction as the state change they describe, and a
background relay (`run_outbox_relay`, wired in `app.main`) publishes them to
JetStream at-least-once.
"""

from herd_common.outbox import OutboxMixin

from app.database import Base


class OutboxEvent(OutboxMixin, Base):
    __tablename__ = "outbox"
