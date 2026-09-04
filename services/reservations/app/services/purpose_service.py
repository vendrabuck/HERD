"""Lab purpose classification: taxonomy validation (phase 1) and AI suggestion
review, backfill, and the terminal-transition marker (phase 2, ADR 0013 points
8-11).

The taxonomy is a plain configured string list (`settings.purpose_categories`),
not a Postgres enum and not a categories table: a row keeps whatever value it
was written with even if that value is later dropped from the configured list
(decision recorded for ADR 0013). `validate_purpose_category` is the one
validation rule every write path (reservation create, the PATCH
purpose-category endpoint, and this module's own accept_purpose_suggestion)
applies.

Phase 2 adds the suggestion lifecycle. The three states reporting and the
admin review surface use are derived, never stored as a separate column:

- unclassified: purpose_category null and no suggestion;
- ai_suggested: purpose_category null and a suggestion present;
- confirmed: purpose_category not null (set by owner or admin).

A reservation becomes eligible for the background classifier the moment
`purpose_classify_requested_at` is non-null; `stamp_purpose_classify_requested`
(called from the five terminal-transition sites) and
`backfill_purpose_classification` (the admin endpoint) are the only two
writers of that column, and both are idempotent (they only ever set it from
null).
"""

import uuid
from datetime import datetime, timezone

from herd_common.pagination import paginate
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.reservation import Reservation, ReservationStatus

# The same three terminal statuses the fork-archive best-effort call and the
# expiration sweep's archive reconciler use (app/tasks/expiration.py's
# TERMINAL_STATUSES). Duplicated here, not imported, to avoid a cycle:
# app/tasks/expiration.py already imports from app/services/reservation_service.py,
# which imports this module.
TERMINAL_STATUSES = (
    ReservationStatus.COMPLETED,
    ReservationStatus.CANCELLED,
    ReservationStatus.FAILED,
)


def validate_purpose_category(value: str | None) -> str | None:
    """Return `value` unchanged if it is None or in the configured taxonomy.

    Raises ValueError with a pinned message otherwise, mirroring the rest of
    this service's business-rule layer (create_reservation and friends raise
    ValueError for a caller-fixable 422; the router maps it to
    HTTPException(422, detail=str(exc))).
    """
    if value is None:
        return None
    allowed = settings.purpose_categories
    if value not in allowed:
        raise ValueError(f"Unknown purpose_category '{value}'; allowed: {', '.join(allowed)}")
    return value


def stamp_purpose_classify_requested(reservation: Reservation) -> None:
    """Mark `reservation` eligible for background purpose classification.

    Sets purpose_classify_requested_at = now() only if it is still null, so
    calling this more than once on the same row (a re-fetch, a defensive
    double-call) is a no-op the second time. Called at every transition into
    COMPLETED, CANCELLED, or FAILED, in the SAME transaction as the status
    change: the five sites are cancel_reservation, release_reservation, and
    the provision-result failure branch in app/services/reservation_service.py,
    plus the auto-complete and dynamic-timeout-failure branches of the
    expiration task's main cycle (app/tasks/expiration.py). This is the ONLY
    way a row becomes eligible for the sweep reconciler, so end-of-reservation
    classification and admin backfill (backfill_purpose_classification below)
    share one mechanism.
    """
    if reservation.purpose_classify_requested_at is None:
        reservation.purpose_classify_requested_at = datetime.now(timezone.utc)


async def list_purpose_review_items(
    db: AsyncSession,
    *,
    skip: int,
    limit: int,
    category: str | None = None,
) -> tuple[list[Reservation], int]:
    """Page through reservations with an undismissed suggestion still worth review.

    Rows: purpose_suggestion is not null, purpose_suggestion_dismissed_at is
    null, and either purpose_category is null or it disagrees with the
    suggestion's top_category (a confirmed row that agrees with the AI is
    dropped from the queue; a confirmed row that disagrees stays, since an
    admin may want to overrule the human pick from this page, per ADR 0013
    point 10). `category`, when given, filters on the suggestion's top_category
    via the JSON path (works on both the SQLite test backend and Postgres,
    the two dialects this service runs against).
    """
    top_category = Reservation.purpose_suggestion["top_category"].as_string()
    disagrees_or_unset = (Reservation.purpose_category.is_(None)) | (
        Reservation.purpose_category != top_category
    )
    stmt = (
        select(Reservation)
        .where(
            Reservation.purpose_suggestion.is_not(None),
            Reservation.purpose_suggestion_dismissed_at.is_(None),
            disagrees_or_unset,
        )
        .order_by(Reservation.purpose_suggested_at.desc(), Reservation.id)
    )
    if category is not None:
        stmt = stmt.where(top_category == category)

    return await paginate(db, stmt, skip=skip, limit=limit)


async def accept_purpose_suggestion(
    db: AsyncSession,
    reservation_id: uuid.UUID,
    admin_id: uuid.UUID,
    purpose_category: str | None,
) -> tuple[Reservation | None, bool]:
    """Accept an AI suggestion (or a chosen override) into purpose_category.

    Returns (reservation, has_suggestion). reservation is None for an unknown
    id (the router 404s). has_suggestion is False when the reservation exists
    but carries no suggestion (the router 409s with the pinned "Reservation has
    no suggestion to accept"); a null `purpose_category` then resolves to the
    suggestion's own top_category, while a non-null value is validated against
    the configured taxonomy (ValueError, mapped to 422 by the router) before
    anything is written. set_by is always the accepting admin, mirroring the
    owner-or-admin PATCH endpoint's set_by/set_at pair, even when the accepted
    value equals what the owner had already picked.
    """
    result = await db.execute(select(Reservation).where(Reservation.id == reservation_id))
    reservation = result.scalar_one_or_none()
    if reservation is None:
        return None, True
    if reservation.purpose_suggestion is None:
        return reservation, False

    resolved = purpose_category
    if resolved is None:
        resolved = reservation.purpose_suggestion.get("top_category")
    validate_purpose_category(resolved)

    reservation.purpose_category = resolved
    reservation.purpose_category_set_by = admin_id
    reservation.purpose_category_set_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(reservation)
    return reservation, True


async def dismiss_purpose_suggestion(
    db: AsyncSession,
    reservation_id: uuid.UUID,
) -> tuple[Reservation | None, bool]:
    """Mark a suggestion reviewed-and-declined so it stops appearing on the review page.

    Returns (reservation, has_suggestion), the same contract as
    accept_purpose_suggestion: None for an unknown id (404), False when the
    reservation has no suggestion (409). The row keeps ai_suggested status
    (purpose_category stays whatever it was); only
    purpose_suggestion_dismissed_at is set.
    """
    result = await db.execute(select(Reservation).where(Reservation.id == reservation_id))
    reservation = result.scalar_one_or_none()
    if reservation is None:
        return None, True
    if reservation.purpose_suggestion is None:
        return reservation, False

    reservation.purpose_suggestion_dismissed_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(reservation)
    return reservation, True


async def backfill_purpose_classification(db: AsyncSession) -> int:
    """Mark every terminal reservation with no suggestion yet as eligible.

    Sets purpose_classify_requested_at = now() on rows in COMPLETED,
    CANCELLED, or FAILED where it is still null AND purpose_suggestion is
    still null. Idempotent: a second call marks zero rows, since every row
    the first call touched now carries a non-null
    purpose_classify_requested_at (and any row the sweep already classified in
    between now also fails the purpose_suggestion IS NULL half). Returns the
    count marked; the sweep reconciler picks these up on its own schedule.
    """
    now = datetime.now(timezone.utc)
    result = await db.execute(
        update(Reservation)
        .where(
            Reservation.status.in_(TERMINAL_STATUSES),
            Reservation.purpose_classify_requested_at.is_(None),
            Reservation.purpose_suggestion.is_(None),
        )
        .values(purpose_classify_requested_at=now)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return result.rowcount
