"""Model package: import every model so Base.metadata sees all tables.

Importing any submodule (or the package) registers both the reservation models
and the outbox table on Base.metadata, so create_all in tests and the lifespan,
plus Alembic autogenerate, pick them up.
"""

from app.models.outbox import OutboxEvent
from app.models.reservation import (
    Reservation,
    ReservationDevice,
    ReservationStatus,
)

__all__ = [
    "OutboxEvent",
    "Reservation",
    "ReservationDevice",
    "ReservationStatus",
]
