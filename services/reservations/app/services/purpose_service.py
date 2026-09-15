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

`classify_purpose_one` (issue #646 phase 2; issue #706 amendment; moved here
from app/tasks/expiration.py by issue #808) is the single-row classifier
call: it opens its own short-lived sessions rather than taking a caller's
AsyncSession, since it runs a read, an HTTP call to the AI orchestrator, and
a write as separate transactions, and it never raises (every outcome, from a
stored suggestion to a transport error, is returned as a string; see its own
docstring for the full outcome taxonomy). It has two callers: the sweep
reconciler (app/tasks/expiration.py's _run_purpose_classify_reconcile, which
enforces purpose_classify_max_attempts in its own SELECT before calling this
function at all) and the admin on-demand trigger
(POST /admin/purpose-review/{id}/classify, issue #808), which calls it
directly for one reservation and therefore does not go through that cap.
"""

import logging
import uuid
from datetime import datetime, timezone

import httpx
from herd_common.internal_client import InternalTokenAuth, call_service
from herd_common.pagination import paginate
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.reservation import Reservation, ReservationDynamicRequest, ReservationStatus

logger = logging.getLogger(__name__)

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


# Purpose-classify reconciler (issue #646 phase 2, ADR 0013; issue #706
# amendment; 2026-09-05 timeout-is-per-row amendment). Status codes the
# orchestrator can answer that mean "try again later", never a per-row
# rejection: a 429 (this caller's or the daily quota's rate limit), a
# 502/503/504 (misconfiguration or an outage, including the
# AI_NOT_CONFIGURED_DETAIL 503 from ai_is_configured()), or any transport
# error OTHER than a timeout (a connection error, most likely). A per-call
# timeout is deliberately excluded from this transient set: see
# classify_purpose_one's "timeout" outcome for why.
_PURPOSE_CLASSIFY_TRANSIENT_STATUS_CODES = frozenset({429, 502, 503, 504})
# The structured 403 marker the orchestrator's flag refusal carries (issue
# #706): only this detail shape (or, for a pre-fix orchestrator image, the
# exact legacy plain-string detail below) means "the feature is off there".
# Any other 403 (an internal-token mismatch, most likely) is a different
# problem and must not be logged or treated as feature-off.
_PURPOSE_CLASSIFICATION_DISABLED_MARKER = "purpose_classification_disabled"
_LEGACY_PURPOSE_CLASSIFICATION_DISABLED_DETAIL = "Purpose classification is disabled"


def _dynamic_requests_classify_payload(
    dynamic_requests: list[ReservationDynamicRequest],
) -> list[dict] | None:
    """Group a reservation's dynamic request rows into {template_id, count}.

    Each ReservationDynamicRequest row is one requested instance (issue #32
    deliberately has no per-row count, so N rows of the same template_id means
    N instances); the classify-purpose contract wants one entry per distinct
    template with its count. None (not an empty list) for a physical-only
    reservation, matching the contract's `[...] | null`.
    """
    if not dynamic_requests:
        return None
    counts: dict[str, int] = {}
    order: list[str] = []
    for dr in dynamic_requests:
        tid = str(dr.template_id)
        if tid not in counts:
            order.append(tid)
        counts[tid] = counts.get(tid, 0) + 1
    return [{"template_id": tid, "count": counts[tid]} for tid in order]


async def _bump_purpose_classify_attempts(reservation_id: uuid.UUID) -> None:
    """Increment purpose_classify_attempts for one row, in its own transaction."""
    async with AsyncSessionLocal() as db:
        res = await db.get(Reservation, reservation_id)
        if res is None:
            return
        res.purpose_classify_attempts += 1
        await db.commit()


def _purpose_classify_403_is_feature_off(resp) -> tuple[bool, object]:
    """Decide whether a 403 from the orchestrator means "the feature is off
    there" (issue #706), and return the detail for logging either way.

    A 403 on this route is reachable two ways: the flag-off refusal, and an
    internal-token mismatch (see docs/AI_PURPOSE_CLASSIFICATION.md and
    services/ai-orchestrator/app/routes/purpose_classification.py). Only the
    flag-off refusal is "not available yet"; a bad token is a configuration
    problem on THIS side (an out-of-sync INTERNAL_API_TOKEN) that will not
    resolve itself on a later tick or a later row. The flag-off refusal is
    the structured detail ``{"error": "purpose_classification_disabled",
    ...}``; a pre-fix orchestrator image instead answers the exact legacy
    plain-string detail, which is accepted as a fallback so a mixed-version
    deployment still reads it as feature-off rather than burning attempts.
    """
    try:
        body = resp.json()
    except ValueError:
        return False, None
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict) and detail.get("error") == _PURPOSE_CLASSIFICATION_DISABLED_MARKER:
        return True, detail
    if detail == _LEGACY_PURPOSE_CLASSIFICATION_DISABLED_DETAIL:
        return True, detail
    return False, detail


async def classify_purpose_one(reservation_id: uuid.UUID) -> str:
    """Classify one reservation's purpose via the AI orchestrator; never raises.

    Returns one of:

    - "ok": a suggestion was stored, or the row was already resolved by a
      concurrent writer.
    - "feature_off": the orchestrator answered 403 carrying the
      purpose-classification-disabled marker (or, for a pre-fix orchestrator
      image, the legacy plain-string detail), meaning
      AI_PURPOSE_CLASSIFICATION_ENABLED is off there; or answered 404,
      meaning the running orchestrator image predates POST
      /internal/classify-purpose (a mixed-version deployment where only
      reservations has been upgraded). Either way the row is left untouched
      and no attempt is counted.
    - "forbidden" (issue #706): the orchestrator answered 403 WITHOUT the
      feature-off marker, most likely an internal-token mismatch on this
      side. Not "not available yet" and not a per-row rejection either: the
      row is left untouched, no attempt counted, but this is logged at
      WARNING (not the feature_off case's INFO) since it is a configuration
      problem an operator needs to see and fix.
    - "timeout" (2026-09-05 amendment, issue #706 follow-up): the call raised
      httpx.TimeoutException. A timeout is per-row evidence, not
      provider-wide evidence: it costs the provider up to
      purpose_classify_timeout_seconds trying to answer THIS row, so it
      counts against the row's own attempt cap (purpose_classify_attempts is
      incremented) and the reconciler loop CONTINUES to the next row rather
      than ending the tick. A row that never finishes in time stops being
      retried once purpose_classify_attempts reaches
      purpose_classify_max_attempts; the admin backfill endpoint resets a
      row's attempts to give it another try. Trade-off, stated honestly: a
      provider that is uniformly slower than the timeout burns every eligible
      row's attempts before ever finishing one, which the backfill endpoint
      recovers from; the alternative (the pre-fix behavior, treating a
      timeout as tick-ending) instead let one slow row stall the whole queue
      behind it forever, which is worse.
    - "transient" (issue #706): the orchestrator answered 429, 502, 503, or
      504 (rate limit, misconfiguration, or an outage, including the
      AI_NOT_CONFIGURED_DETAIL 503 case), or the call raised a transport
      error OTHER than a timeout (e.g. httpx.ConnectError). The row is left
      untouched and no attempt is counted: a sustained transient condition
      would otherwise burn a row's whole attempt cap before it ever gets a
      real classification try.
    - "failed": any other non-200 status, a 200 with an unparseable body,
      purpose_classify_attempts is incremented.

    Each outcome that mutates the row does so in its own session/commit, so
    one row's failure never affects another row in the same batch.

    Two callers (issue #808): app/tasks/expiration.py's
    _run_purpose_classify_reconcile (the sweep, which enforces
    purpose_classify_max_attempts before calling this at all) and
    routers/purpose_review.py's POST /admin/purpose-review/{id}/classify (the
    on-demand trigger, which does not check that cap: it is the operator's
    way to retry one exhausted row directly). Concurrency between the two is
    accepted, not locked: if the sweep and the trigger classify the same row
    at the same time, both write the same suggestion shape and the last
    commit wins; there is no claim column.
    """
    async with AsyncSessionLocal() as db:
        res = await db.get(Reservation, reservation_id)
        if res is None or res.purpose_suggestion is not None:
            # Already resolved by a concurrent writer (another instance's
            # sweep tick, or an admin action) since this row was selected for
            # the batch: nothing to do.
            return "ok"
        payload = {
            "reservation_id": str(res.id),
            "categories": list(settings.purpose_categories),
            "purpose": res.purpose,
            "user_id": str(res.user_id),
            "device_ids": [str(d) for d in res.device_ids],
            "topology_id": str(res.topology_id) if res.topology_id else None,
            "dynamic_requests": _dynamic_requests_classify_payload(res.dynamic_requests),
            "start_time": res.start_time.isoformat(),
            "end_time": res.end_time.isoformat(),
            "status": res.status.value,
        }

    try:
        resp = await call_service(
            settings.ai_orchestrator_service_url,
            "POST",
            "/internal/classify-purpose",
            json_body=payload,
            timeout=settings.purpose_classify_timeout_seconds,
            auth=InternalTokenAuth(token=settings.internal_api_token),
        )
    except httpx.TimeoutException:
        logger.warning(
            "Purpose classify reconcile: call to the orchestrator timed out for "
            "%s after %s seconds; a timeout is per-row evidence, not a "
            "provider-wide outage, so it counts against this row's attempt cap "
            "and the reconciler continues to the next row",
            reservation_id,
            settings.purpose_classify_timeout_seconds,
            extra={
                "action": "purpose_classify_timeout",
                "reservation_id": str(reservation_id),
                "timeout_seconds": settings.purpose_classify_timeout_seconds,
            },
        )
        await _bump_purpose_classify_attempts(reservation_id)
        return "timeout"
    except Exception:
        logger.warning(
            "Purpose classify reconcile: call to the orchestrator failed for %s "
            "(a transport error, not a timeout); treating this as transient for "
            "this tick, no attempt counted",
            reservation_id,
            exc_info=True,
            extra={
                "action": "purpose_classify_transient",
                "reservation_id": str(reservation_id),
                "status_code": None,
            },
        )
        return "transient"

    if resp.status_code in _PURPOSE_CLASSIFY_TRANSIENT_STATUS_CODES:
        logger.warning(
            "Purpose classify reconcile: orchestrator returned %s for %s "
            "(rate limit, misconfiguration, or an outage); treating this as "
            "transient for this tick, no attempt counted",
            resp.status_code,
            reservation_id,
            extra={
                "action": "purpose_classify_transient",
                "reservation_id": str(reservation_id),
                "status_code": resp.status_code,
            },
        )
        return "transient"

    if resp.status_code == 403:
        is_feature_off, detail = _purpose_classify_403_is_feature_off(resp)
        if is_feature_off:
            logger.info(
                "Purpose classify reconcile: the orchestrator answered 403 for %s "
                "(AI_PURPOSE_CLASSIFICATION_ENABLED is off there); treating this as "
                "feature-off for this tick",
                reservation_id,
                extra={
                    "action": "purpose_classify_feature_off",
                    "reservation_id": str(reservation_id),
                    "status_code": 403,
                },
            )
            return "feature_off"
        logger.warning(
            "Purpose classify reconcile: orchestrator returned 403 for %s that is "
            "NOT the feature-off marker (detail=%r); this looks like an "
            "internal-token mismatch, not the flag being off; ending this tick, "
            "no attempt counted",
            reservation_id,
            detail,
            extra={
                "action": "purpose_classify_forbidden",
                "reservation_id": str(reservation_id),
                "status_code": 403,
            },
        )
        return "forbidden"

    if resp.status_code == 404:
        # The running orchestrator image predates this endpoint entirely (a
        # mixed-version deployment mid-upgrade, or a stack where only
        # reservations was updated). "Not available yet", not a per-row
        # failure, so no attempt is counted; kept distinguishable from the 403
        # case in the log message so an operator can tell a flag flip from a
        # stale image.
        logger.info(
            "Purpose classify reconcile: the orchestrator answered 404 for %s "
            "(it does not expose POST /internal/classify-purpose yet); treating "
            "this as feature-off for this tick",
            reservation_id,
            extra={
                "action": "purpose_classify_feature_off",
                "reservation_id": str(reservation_id),
                "status_code": 404,
            },
        )
        return "feature_off"

    if resp.status_code != 200:
        logger.warning(
            "Purpose classify reconcile: orchestrator returned %s for %s",
            resp.status_code,
            reservation_id,
            extra={
                "action": "purpose_classify_bad_status",
                "reservation_id": str(reservation_id),
                "status_code": resp.status_code,
            },
        )
        await _bump_purpose_classify_attempts(reservation_id)
        return "failed"

    try:
        suggestion = resp.json()
    except ValueError:
        logger.warning(
            "Purpose classify reconcile: unparseable 200 body for %s",
            reservation_id,
            extra={"action": "purpose_classify_bad_body", "reservation_id": str(reservation_id)},
        )
        await _bump_purpose_classify_attempts(reservation_id)
        return "failed"

    async with AsyncSessionLocal() as db:
        res = await db.get(Reservation, reservation_id)
        if res is None:
            return "ok"
        res.purpose_suggestion = suggestion
        res.purpose_suggested_at = datetime.now(timezone.utc)
        await db.commit()
    logger.info(
        "Purpose classify reconcile: stored a suggestion for %s",
        reservation_id,
        extra={"action": "purpose_classify_stored", "reservation_id": str(reservation_id)},
    )
    return "ok"


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
    """Mark every terminal reservation with no suggestion yet as eligible, and
    reset any row the sweep has already given up on so it gets another run.

    Two independent updates, both counted into the single returned total:

    1. Sets purpose_classify_requested_at = now() on rows in COMPLETED,
       CANCELLED, or FAILED where it is still null AND purpose_suggestion is
       still null. Idempotent on its own: a second call marks zero of these,
       since every row the first call touched now carries a non-null
       purpose_classify_requested_at (and any row the sweep already
       classified in between now also fails the purpose_suggestion IS NULL
       half).
    2. Resets purpose_classify_attempts to 0 on rows that hit the sweep's
       attempt cap (purpose_classify_attempts >= purpose_classify_max_attempts)
       and still have no suggestion. A capped row already carries a non-null
       purpose_classify_requested_at from whenever it was first picked up, so
       resetting only the attempt counter (not the timestamp) is enough to
       make the reconciler's `attempts < max_attempts` filter select it again,
       and it keeps its place in the oldest-requested-first ordering rather
       than jumping to the back of the queue. This closes the gap where a
       stack running a mismatched orchestrator image (see
       _classify_purpose_one's 403-vs-404 handling) could burn every
       historical row's attempts to the cap within a few sweep ticks, with no
       way to retry them short of a manual database edit.

    The two updates target disjoint rows (the first requires
    purpose_classify_requested_at IS NULL, the second requires attempts at or
    over the cap, which only happens after that column is set), so the
    combined count never double-counts a row. Returns the total rows touched
    by either update; the sweep reconciler picks all of them up on its own
    schedule.
    """
    now = datetime.now(timezone.utc)
    newly_marked = await db.execute(
        update(Reservation)
        .where(
            Reservation.status.in_(TERMINAL_STATUSES),
            Reservation.purpose_classify_requested_at.is_(None),
            Reservation.purpose_suggestion.is_(None),
        )
        .values(purpose_classify_requested_at=now)
        .execution_options(synchronize_session=False)
    )
    reset_capped = await db.execute(
        update(Reservation)
        .where(
            Reservation.status.in_(TERMINAL_STATUSES),
            Reservation.purpose_classify_requested_at.is_not(None),
            Reservation.purpose_suggestion.is_(None),
            Reservation.purpose_classify_attempts >= settings.purpose_classify_max_attempts,
        )
        .values(purpose_classify_attempts=0)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return newly_marked.rowcount + reset_capped.rowcount
