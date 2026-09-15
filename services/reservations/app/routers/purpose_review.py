"""Admin surface for AI purpose suggestions (issue #646 phase 2, ADR 0013 point 10;
issue #808's on-demand trigger).

Owner-set categories are confirmed on write (the PATCH endpoint in
routers/reservations.py); AI suggestions wait here for an admin to accept,
override, or dismiss. All five endpoints are admin-only. Kept as a separate
router (mounted with no prefix, same as reservations.py) rather than folded
into routers/reservations.py, which is already large.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies.auth import require_admin
from app.models.reservation import Reservation
from app.schemas.reservation import (
    PurposeBackfillResponse,
    PurposeClassifyTriggerResponse,
    PurposeReviewAcceptBody,
    PurposeReviewItem,
    PurposeReviewListResponse,
    ReservationResponse,
)
from app.services.purpose_service import (
    accept_purpose_suggestion,
    backfill_purpose_classification,
    classify_purpose_one,
    dismiss_purpose_suggestion,
    list_purpose_review_items,
)

# The same structured marker classify_purpose_one uses to recognize the
# orchestrator's flag-off refusal (app/tasks/expiration.py's
# _PURPOSE_CLASSIFICATION_DISABLED_MARKER duplicated the same way; this
# endpoint answers its OWN caller with it rather than relaying the
# orchestrator's body, so it is pinned here too, matching how other AI-gated
# endpoints in this codebase answer when unconfigured).
CLASSIFICATION_DISABLED_DETAIL = {"error": "purpose_classification_disabled"}

router = APIRouter(tags=["purpose-review"])

# Pinned wording (contract, issue #646 phase 2): identical for both accept and
# dismiss, since both mean "there is nothing here to review yet".
NO_SUGGESTION_DETAIL = "Reservation has no suggestion to accept"


@router.get("/admin/purpose-review", response_model=PurposeReviewListResponse)
async def list_purpose_review(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    category: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(require_admin),
):
    """Paginated queue of undismissed AI suggestions worth a look (ADR 0013 point 10).

    Excludes a confirmed row that agrees with its suggestion (nothing to
    review); includes one that disagrees (an admin may want to overrule the
    owner's pick); excludes any dismissed suggestion. `category` filters on
    the suggestion's top_category.
    """
    items, total = await list_purpose_review_items(db, skip=skip, limit=limit, category=category)
    return PurposeReviewListResponse(
        items=[
            PurposeReviewItem(
                reservation_id=r.id,
                user_id=r.user_id,
                purpose=r.purpose,
                start_time=r.start_time,
                end_time=r.end_time,
                status=r.status,
                purpose_category=r.purpose_category,
                purpose_suggestion=r.purpose_suggestion,
                purpose_suggested_at=r.purpose_suggested_at,
                device_count=len(r.device_ids),
            )
            for r in items
        ],
        total=total,
        skip=skip,
        limit=limit,
    )


@router.post(
    "/admin/purpose-review/{reservation_id}/accept",
    response_model=ReservationResponse,
)
async def accept_purpose_review(
    reservation_id: uuid.UUID,
    body: PurposeReviewAcceptBody,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin),
):
    """Accept a suggestion (or a chosen override) into purpose_category.

    A null body accepts the suggestion's own top_category. 404 for an unknown
    reservation; 409 when the reservation has no suggestion to accept.
    """
    admin_id = uuid.UUID(admin["sub"])
    try:
        reservation, has_suggestion = await accept_purpose_suggestion(
            db, reservation_id, admin_id, body.purpose_category
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if reservation is None:
        raise HTTPException(status_code=404, detail="Reservation not found")
    if not has_suggestion:
        raise HTTPException(status_code=409, detail=NO_SUGGESTION_DETAIL)
    return reservation


@router.post(
    "/admin/purpose-review/{reservation_id}/dismiss",
    response_model=ReservationResponse,
)
async def dismiss_purpose_review(
    reservation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(require_admin),
):
    """Mark a suggestion reviewed-and-declined so it stops surfacing on this page.

    404 for an unknown reservation; 409 when the reservation has no
    suggestion. The row keeps its suggestion (agreement-rate metrics stay
    computable); only purpose_suggestion_dismissed_at is set.
    """
    reservation, has_suggestion = await dismiss_purpose_suggestion(db, reservation_id)
    if reservation is None:
        raise HTTPException(status_code=404, detail="Reservation not found")
    if not has_suggestion:
        raise HTTPException(status_code=409, detail=NO_SUGGESTION_DETAIL)
    return reservation


@router.post("/admin/purpose/backfill", response_model=PurposeBackfillResponse)
async def backfill_purpose_review(
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(require_admin),
):
    """Mark every terminal reservation with no suggestion yet as eligible for the
    sweep, and reset any row that hit the sweep's attempt cap so it gets
    another run.

    Two things happen under one count: rows never requested get
    purpose_classify_requested_at stamped, and rows already at
    purpose_classify_attempts >= the configured max (and still without a
    suggestion) have their attempt counter reset to 0, which is what un-sticks
    a row after a transient outage (e.g. a mixed-version deployment where the
    orchestrator did not yet expose the classify endpoint) burned through its
    retries. Idempotent: a second call returns {"marked": 0} once the first
    call's rows have all either picked up a purpose_classify_requested_at
    timestamp or (for the capped case) gone on to either get classified or
    exhaust the cap again on their own.
    """
    marked = await backfill_purpose_classification(db)
    return PurposeBackfillResponse(marked=marked)


@router.post(
    "/admin/purpose-review/{reservation_id}/classify",
    response_model=PurposeClassifyTriggerResponse,
)
async def trigger_purpose_classify(
    reservation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _admin: dict = Depends(require_admin),
):
    """Classify one reservation's purpose right now (issue #808).

    An on-demand escape hatch for the sweep reconciler
    (app.tasks.expiration._run_purpose_classify_reconcile): an operator who
    wants a suggestion for ONE reservation now, without waiting for that
    reservation's turn in the sweep's oldest-requested-first queue, calls
    this instead. It runs the exact same single-row classifier the sweep
    uses, app.services.purpose_service.classify_purpose_one, synchronously,
    for exactly this reservation_id.

    404 for an unknown reservation. 409 {"error": "not_eligible"} when
    purpose_classify_requested_at is still null: the reservation has not
    reached a terminal state (COMPLETED, CANCELLED, or FAILED), so there is
    nothing eligible to classify yet. 409 {"error": "already_suggested"}
    when purpose_suggestion is already set: dismiss or override the existing
    suggestion through the accept/dismiss endpoints above before asking for
    a new one. 503 {"error": "purpose_classification_disabled"} when the
    classifier answers "feature_off" (AI_PURPOSE_CLASSIFICATION_ENABLED is
    off on the orchestrator, or the running orchestrator image predates the
    endpoint), matching how AI-gated endpoints elsewhere answer when
    unconfigured.

    Every OTHER outcome ("ok", "timeout", "transient", "failed", or
    "forbidden") is still a 200: the call itself succeeded, the
    classification attempt did or did not, and the response's outcome field
    says which. Attempt bookkeeping is exactly the sweep's own
    (classify_purpose_one increments purpose_classify_requested_at's sibling
    purpose_classify_attempts on "timeout" and "failed", the two outcomes
    that count against it; "transient" and "forbidden" do not, the same as
    the sweep).

    Deliberately ignores purpose_classify_max_attempts: this endpoint is the
    operator's way to retry one exhausted row directly, without running the
    global POST /admin/purpose/backfill (the sweep's own SELECT is what
    enforces that cap, not classify_purpose_one; see
    app/tasks/expiration.py's _run_purpose_classify_reconcile).

    Concurrency with the sweep is accepted, not locked: the sweep may be
    classifying this same row in the same window (it is eligible the moment
    purpose_classify_requested_at is set, whether or not an operator also
    triggers it by hand). Both write the same suggestion shape through
    independent short sessions; the last commit wins. No claim column, no
    row lock.
    """
    reservation = await db.get(Reservation, reservation_id)
    if reservation is None:
        raise HTTPException(status_code=404, detail="Reservation not found")
    if reservation.purpose_classify_requested_at is None:
        raise HTTPException(status_code=409, detail={"error": "not_eligible"})
    if reservation.purpose_suggestion is not None:
        raise HTTPException(status_code=409, detail={"error": "already_suggested"})

    outcome = await classify_purpose_one(reservation_id)
    if outcome == "feature_off":
        raise HTTPException(status_code=503, detail=CLASSIFICATION_DISABLED_DETAIL)

    # classify_purpose_one committed through its own session(s); refresh
    # this request's copy rather than trusting the identity map's pre-call
    # values (the same stale-read hazard documented for cabling's
    # _load_fork(..., refresh=True), issue #626).
    await db.refresh(reservation)
    return PurposeClassifyTriggerResponse(
        reservation_id=reservation_id,
        outcome=outcome,
        purpose_suggestion=reservation.purpose_suggestion,
    )
