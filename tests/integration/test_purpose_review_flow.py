"""End-to-end integration: AI purpose-suggestion review and backfill (issue
#646 phase 2, ADR 0013 points 8-11).

Assumes a running HERD stack (make up / make everything's ephemeral stack).
The dev/test override pins EXPIRATION_INTERVAL_SECONDS=5, so the sweep
reconciler ticks often enough for the AI-gated test's poll loop to fit inside
a normal test timeout.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from _ai_helpers import ai_provider_configured

pytestmark = pytest.mark.asyncio

POLL_TIMEOUT_SECONDS = 60
POLL_INTERVAL_SECONDS = 3


def _reservation_body(device_id: str) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "device_ids": [device_id],
        "purpose": "replicating a customer support case against the FRR driver",
        "start_time": now.isoformat(),
        "end_time": (now + timedelta(minutes=5)).isoformat(),
    }


@pytest.mark.seeded_skip_ok("needs AI_* env")
async def test_cancelled_reservation_gets_a_suggestion_visible_in_admin_review(
    admin_client, fresh_device
):
    """A cancelled reservation is stamped eligible, the sweep reconciler
    classifies it via the live AI provider, and the suggestion surfaces on
    the admin review list (ADR 0013 point 10). Skipped when no AI provider is
    configured on this host (nightly and the plain gate stack have no AI_*
    env): the sweep would otherwise never produce a suggestion to poll for.
    """
    if not ai_provider_configured():
        pytest.skip("AI provider not configured on this host; sweep classifier not exercised")

    create = await admin_client.post("/reservations/", json=_reservation_body(fresh_device["id"]))
    assert create.status_code == 201, create.text
    reservation_id = create.json()["id"]

    try:
        cancel = await admin_client.delete(f"/reservations/{reservation_id}")
        assert cancel.status_code == 204, cancel.text

        deadline = asyncio.get_event_loop().time() + POLL_TIMEOUT_SECONDS
        found = None
        while asyncio.get_event_loop().time() < deadline:
            resp = await admin_client.get(
                "/reservations/admin/purpose-review", params={"limit": 200}
            )
            assert resp.status_code == 200, resp.text
            found = next(
                (i for i in resp.json()["items"] if i["reservation_id"] == reservation_id),
                None,
            )
            if found is not None:
                break
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

        assert found is not None, (
            "reservation never surfaced on the admin review list within "
            f"{POLL_TIMEOUT_SECONDS}s; check the reservations service logs for "
            "action=purpose_classify_* and confirm AI_PURPOSE_CLASSIFICATION_ENABLED "
            "is set on the ai-orchestrator container"
        )
        assert found["purpose_suggestion"]["top_category"]
        assert found["purpose_category"] is None
    finally:
        # Best-effort: the reservation is already CANCELLED (terminal), so
        # this is just cleanup of the admin review queue, not a state check.
        await admin_client.post(f"/reservations/admin/purpose-review/{reservation_id}/dismiss")


async def test_backfill_marks_and_is_idempotent(admin_client, fresh_device):
    """POST /admin/purpose/backfill returns {"marked": n} and a second call
    against the same rows returns 0 (issue #646 phase 2)."""
    create = await admin_client.post("/reservations/", json=_reservation_body(fresh_device["id"]))
    assert create.status_code == 201, create.text
    reservation_id = create.json()["id"]

    try:
        cancel = await admin_client.delete(f"/reservations/{reservation_id}")
        assert cancel.status_code == 204, cancel.text

        # The DELETE above already stamped purpose_classify_requested_at (it is
        # one of the five terminal-transition sites), so backfill's own count
        # is not asserted exactly (other terminal reservations on a shared
        # stack may also be freshly eligible); the idempotency property is
        # what this test pins.
        first = await admin_client.post("/reservations/admin/purpose/backfill")
        assert first.status_code == 200, first.text
        assert isinstance(first.json()["marked"], int)

        second = await admin_client.post("/reservations/admin/purpose/backfill")
        assert second.status_code == 200, second.text
        assert second.json()["marked"] == 0
    finally:
        await admin_client.post(f"/reservations/admin/purpose-review/{reservation_id}/dismiss")


async def test_purpose_review_list_is_admin_only(user_client):
    resp = await user_client.get("/reservations/admin/purpose-review")
    assert resp.status_code == 403


async def test_purpose_backfill_is_admin_only(user_client):
    resp = await user_client.post("/reservations/admin/purpose/backfill")
    assert resp.status_code == 403
