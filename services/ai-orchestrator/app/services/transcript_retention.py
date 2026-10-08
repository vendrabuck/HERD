"""Whether the idle-conversation sweeper must keep a reservation's transcript.

Issue #1039: the end-of-reservation purpose classification reads the
reservation's assistant conversations (purpose_signals._gather_transcripts_block),
so a conversation idle past ASSISTANT_CONVERSATION_TTL_HOURS must not be
deleted while that read is still owed. The sweeper keeps a conversation when
its reservation is not terminal yet, or is terminal with the classification
still pending (purpose_classify_requested_at set, no suggestion, and the
purpose sweep's attempts not yet at purpose_classify_max_attempts, reported by
reservations as `purpose_classification_pending`; issue #1067 added the cap, so
a row the sweep has given up on releases its transcript).

The exemption applies only while this service would actually send transcripts
to the classifier (AI_PURPOSE_CLASSIFICATION_ENABLED and
AI_PURPOSE_INCLUDE_TRANSCRIPTS both on); otherwise nothing will ever read the
transcript and the plain idle TTL applies, with no lookup at all.

The answer comes from reservations' internal GET /internal/{id} with the
internal token and fails closed: anything other than a clear answer (a
transport error, a non-200 other than 404, a malformed body, an unknown
status, a missing token) keeps the conversation for the next cycle. A 404 is a
clear answer: the reservation does not exist, so no classification will read
the transcript.
"""

from __future__ import annotations

import logging
import uuid

import httpx
from herd_common.internal_client import InternalTokenAuth, call_service

from app.config import settings

logger = logging.getLogger(__name__)

LOOKUP_TIMEOUT_SECONDS = 10.0
LOOKUP_CONCURRENCY = 8

LIVE_STATUSES = frozenset({"PENDING", "PENDING_PROVISION", "ACTIVE"})
TERMINAL_STATUSES = frozenset({"COMPLETED", "CANCELLED", "FAILED"})


def transcripts_owed_to_classifier() -> bool:
    """True when the end-of-reservation pass would read assistant transcripts."""
    return bool(
        settings.ai_purpose_classification_enabled and settings.ai_purpose_include_transcripts
    )


def _lookup_failed(reservation_id: uuid.UUID, reason: str, **fields: object) -> bool:
    logger.warning(
        "conversation_retention_lookup_failed",
        extra={"reservation_id": str(reservation_id), "reason": reason, **fields},
    )
    return True


async def reservation_keeps_transcript(reservation_id: uuid.UUID) -> bool:
    """True when the reservation's idle conversations must be kept."""
    try:
        resp = await call_service(
            settings.reservations_service_url.rstrip("/"),
            "GET",
            f"/internal/{reservation_id}",
            timeout=LOOKUP_TIMEOUT_SECONDS,
            auth=InternalTokenAuth(settings.internal_api_token),
        )
    except (httpx.HTTPError, RuntimeError) as exc:
        return _lookup_failed(reservation_id, "unreachable", error_class=type(exc).__name__)
    if resp.status_code == 404:
        return False
    if resp.status_code != 200:
        return _lookup_failed(reservation_id, "status", status_code=resp.status_code)
    try:
        body = resp.json()
    except ValueError:
        return _lookup_failed(reservation_id, "malformed")
    if not isinstance(body, dict):
        return _lookup_failed(reservation_id, "malformed")
    status = body.get("status")
    if status in LIVE_STATUSES:
        return True
    if status in TERMINAL_STATUSES:
        pending = body.get("purpose_classification_pending")
        if not isinstance(pending, bool):
            return _lookup_failed(reservation_id, "malformed")
        return pending
    return _lookup_failed(reservation_id, "unknown_status")
