"""Dynamic-instance ledger transitions (ADR 0004, issue #32).

Thin state helpers over the dynamic_instances table, the applied-state ledger
the create and teardown flows drive from. Every mutation re-reads the row by
its unique request_id inside the caller's session, so a helper can be called
across separate sessions (the consumer opens a fresh session per side effect,
the same idiom as vlan_service and route_service).

Status lifecycle: CREATING to ACTIVE (create_instance succeeded, device
materialized) to DESTROYED (destroy_instance plus device delete done). A row
stuck in CREATING is retried idempotently; the unique request_id makes a
concurrent insert lose to IntegrityError and re-read the winner. The lifecycle
only moves forward: set_instance_ref and mark_active are compare-and-swap
updates that never touch a DESTROYED row (issue #896). A row becomes DESTROYED
only after the recipe's destroy_instance reported success, by instance_ref or,
for a row with none, keyed by HERD_request_id (issue #937); a row whose create
outcome is unknown is never retired as "nothing to destroy".
"""

import logging

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dynamic_instance import DynamicInstance
from app.services._uuid_utils import as_uuid as _as_uuid

logger = logging.getLogger(__name__)

# Statuses from which a row may still move forward. DESTROYED is terminal.
LIVE_STATUSES = ("CREATING", "ACTIVE")


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


async def set_instance_ref(db: AsyncSession, request_id, instance_ref: str | None) -> bool:
    """Record the hypervisor-side instance_ref while the row is still live.

    Persisted before the inventory device-create so teardown can still destroy
    the hypervisor-side instance if the device-create call fails and NAKs.

    A compare-and-swap (issue #896): the UPDATE applies only while the row is
    CREATING or ACTIVE, so a row a concurrent teardown already retired to
    DESTROYED is never touched. Returns True when this call won the row; False
    means the row is DESTROYED or absent and the caller owns a hypervisor
    instance no ledger row tracks (it must destroy it).
    """
    result = await db.execute(
        update(DynamicInstance)
        .where(
            DynamicInstance.request_id == _as_uuid(request_id),
            DynamicInstance.status.in_(LIVE_STATUSES),
        )
        .values(instance_ref=instance_ref)
    )
    await db.commit()
    return result.rowcount == 1


async def mark_active(db: AsyncSession, request_id, device_id, instance_ref: str | None) -> bool:
    """Flip a row to ACTIVE once the instance is materialized as a device.

    Same compare-and-swap as set_instance_ref (issue #896): a DESTROYED row
    never comes back to life. Returns True when this call won the row, False
    when teardown retired it first (the caller must undo what it created).
    """
    result = await db.execute(
        update(DynamicInstance)
        .where(
            DynamicInstance.request_id == _as_uuid(request_id),
            DynamicInstance.status.in_(LIVE_STATUSES),
        )
        .values(
            device_id=_as_uuid(device_id),
            instance_ref=instance_ref,
            status="ACTIVE",
            error=None,
        )
    )
    await db.commit()
    return result.rowcount == 1


async def mark_destroyed(
    db: AsyncSession, request_id, *, instance_ref: str | None, device_id
) -> bool:
    """Flip a row to DESTROYED, compare-and-swap on the snapshot teardown acted on.

    The caller must hold a successful destroy_instance result for this row (by
    instance_ref, or keyed by request id when the row has none): DESTROYED means
    the driver destroyed the instance or confirmed none exists (issue #937).

    Teardown drives the recipe for minutes between reading the row and calling
    this, and a create on another replica can land in that window (record an
    instance_ref, materialize a device, flip ACTIVE). So the UPDATE applies only
    while the row is still live AND its instance_ref and device_id still equal
    the values teardown read (`instance_ref`, `device_id`; None matches only
    NULL, the same on Postgres and SQLite). Returns True when this call retired
    the row; False means the row changed or is gone, and the caller must re-read
    it and tear down what it holds now, never retire it from the stale snapshot.
    """

    def _same(column, value):
        return column.is_(None) if value is None else column == value

    result = await db.execute(
        update(DynamicInstance)
        .where(
            DynamicInstance.request_id == _as_uuid(request_id),
            DynamicInstance.status.in_(LIVE_STATUSES),
            _same(DynamicInstance.instance_ref, instance_ref),
            _same(DynamicInstance.device_id, None if device_id is None else _as_uuid(device_id)),
        )
        .values(status="DESTROYED")
    )
    await db.commit()
    return result.rowcount == 1


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
