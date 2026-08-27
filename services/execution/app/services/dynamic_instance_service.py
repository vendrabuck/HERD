"""Dynamic-instance ledger transitions (ADR 0004, issue #32).

Thin state helpers over the dynamic_instances table, the applied-state ledger
the create and teardown flows drive from. Every mutation re-reads the row by
its unique request_id inside the caller's session, so a helper can be called
across separate sessions (the consumer opens a fresh session per side effect,
the same idiom as vlan_service and route_service).

Status lifecycle: CREATING to ACTIVE (create_instance succeeded, device
materialized) to DESTROYED (destroy_instance plus device delete done). A row
stuck in CREATING is retried idempotently; the unique request_id makes a
concurrent insert lose to IntegrityError and re-read the winner.
"""

import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dynamic_instance import DynamicInstance
from app.services._uuid_utils import as_uuid as _as_uuid

logger = logging.getLogger(__name__)


async def get_by_request_id(db: AsyncSession, request_id) -> DynamicInstance | None:
    """Return the ledger row for a request_id, or None if none exists yet."""
    result = await db.execute(
        select(DynamicInstance).where(DynamicInstance.request_id == _as_uuid(request_id))
    )
    return result.scalar_one_or_none()


async def insert_or_get_creating(
    db: AsyncSession,
    request_id,
    reservation_id,
    template_id,
    hypervisor_id,
) -> DynamicInstance:
    """Insert a CREATING row for a request, or return the existing row.

    Idempotent under NATS redelivery: an existing row (any status) is returned
    as-is. The unique request_id makes the database the arbiter; a racing
    concurrent insert trips IntegrityError, so we roll back and re-read the
    winner's row.
    """
    existing = await get_by_request_id(db, request_id)
    if existing is not None:
        return existing

    row = DynamicInstance(
        request_id=_as_uuid(request_id),
        reservation_id=_as_uuid(reservation_id),
        template_id=_as_uuid(template_id),
        hypervisor_id=_as_uuid(hypervisor_id),
        status="CREATING",
    )
    db.add(row)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        existing = await get_by_request_id(db, request_id)
        if existing is None:
            # The unique violation implies a row exists; a None here is a real
            # anomaly worth surfacing rather than silently retrying forever.
            raise
        return existing
    await db.refresh(row)
    return row


async def set_instance_ref(db: AsyncSession, request_id, instance_ref: str | None) -> None:
    """Record the hypervisor-side instance_ref while the row is still CREATING.

    Persisted before the inventory device-create so teardown can still destroy
    the hypervisor-side instance if the device-create call fails and NAKs.
    """
    row = await get_by_request_id(db, request_id)
    if row is None:
        return
    row.instance_ref = instance_ref
    await db.commit()


async def mark_active(db: AsyncSession, request_id, device_id, instance_ref: str | None) -> None:
    """Flip a row to ACTIVE once the instance is materialized as a device."""
    row = await get_by_request_id(db, request_id)
    if row is None:
        return
    row.device_id = _as_uuid(device_id)
    row.instance_ref = instance_ref
    row.status = "ACTIVE"
    row.error = None
    await db.commit()


async def mark_destroyed(db: AsyncSession, request_id) -> None:
    """Flip a row to DESTROYED after destroy_instance and the device delete."""
    row = await get_by_request_id(db, request_id)
    if row is None:
        return
    row.status = "DESTROYED"
    await db.commit()


async def list_teardown_candidates(db: AsyncSession, reservation_id) -> list[DynamicInstance]:
    """Return the reservation's rows still in CREATING or ACTIVE.

    DESTROYED rows are excluded, so a redelivered teardown is a no-op for
    instances already torn down. With expire_on_commit=False the returned rows
    keep their loaded attributes after the session closes, so the caller can
    read them outside this session.
    """
    result = await db.execute(
        select(DynamicInstance).where(
            DynamicInstance.reservation_id == _as_uuid(reservation_id),
            DynamicInstance.status.in_(("CREATING", "ACTIVE")),
        )
    )
    return list(result.scalars().all())
