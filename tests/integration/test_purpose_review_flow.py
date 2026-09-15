"""End-to-end integration: AI purpose-suggestion review and backfill (issue
#646 phase 2, ADR 0013 points 8-11), plus the on-demand per-reservation
trigger (issue #808).

Assumes a running HERD stack (make up / make everything's ephemeral stack).
The dev/test override pins EXPIRATION_INTERVAL_SECONDS=5, so the sweep
reconciler ticks often enough for the backfill test in this file to fit
inside a normal test timeout.

History (issue #808): this file's first test used to wait on the global
sweep reconciler, which classifies its backlog oldest-`purpose_classify_
requested_at`-first and serially, at model speed, one row per tick. On a
reused dev stack carrying a purpose-classify backlog from earlier runs, the
sweep could take longer than the test's poll budget to reach this test's own
reservation, purely from queue position, not a reconciler defect (observed
twice, 2026-09-12 and 2026-09-13; see issue #808). The fix is
POST /admin/purpose-review/{id}/classify (issue #808): the test now triggers
classification for its own reservation directly instead of waiting for the
sweep's turn, so it no longer depends on the sweep's backlog or on running
before the rest of this suite. `@pytest.mark.classify_sweep_first` and its
supporting `tests/integration/conftest.py` reordering hook are removed by
the same change: this was their only consumer (confirmed by grep across the
repo before removal).
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from _ai_helpers import ai_provider_configured

pytestmark = pytest.mark.asyncio

POLL_TIMEOUT_SECONDS = 15
POLL_INTERVAL_SECONDS = 2


def _reservation_body(device_id: str, *, future: bool = False) -> dict:
    """A reservation starting now (the default, matching the original test's
    shape: it starts immediately so it can be cancelled right away) or, with
    future=True, starting in an hour so it stays PENDING (not yet terminal)
    for the not_eligible test.
    """
    now = datetime.now(timezone.utc)
    start = now + timedelta(hours=1) if future else now
    end = start + timedelta(minutes=5)
    return {
        "device_ids": [device_id],
        "purpose": "replicating a customer support case against the FRR driver",
        "start_time": start.isoformat(),
        "end_time": end.isoformat(),
    }


@pytest.mark.seeded_skip_ok("needs AI_* env")
@pytest.mark.timeout(60)
async def test_cancelled_reservation_gets_a_suggestion_visible_in_admin_review(
    admin_client, fresh_device
):
    """A cancelled reservation is stamped eligible, the on-demand trigger
    (issue #808) classifies it synchronously via the live AI provider, and
    the suggestion surfaces on the admin review list (ADR 0013 point 10).
    Skipped when no AI provider is configured on this host (nightly and the
    plain gate stack have no AI_* env): the trigger would otherwise always
    answer feature_off.

    `@pytest.mark.timeout(60)` overrides the suite's global `--timeout=30`:
    the trigger's own call to the orchestrator is bounded by
    purpose_classify_timeout_seconds (default 30s, and the local vLLM setup
    this test was written against has been observed answering in 7 to 50s),
    plus POLL_TIMEOUT_SECONDS (15s) for the read-after-write poll below, plus
    overhead for the create/cancel/dismiss calls around both.
    """
    if not ai_provider_configured():
        pytest.skip("AI provider not configured on this host; classifier not exercised")

    create = await admin_client.post("/reservations/", json=_reservation_body(fresh_device["id"]))
    assert create.status_code == 201, create.text
    reservation_id = create.json()["id"]

    try:
        cancel = await admin_client.delete(f"/reservations/{reservation_id}")
        assert cancel.status_code == 204, cancel.text

        trigger = await admin_client.post(
            f"/reservations/admin/purpose-review/{reservation_id}/classify"
        )
        assert trigger.status_code == 200, trigger.text
        trigger_body = trigger.json()
        assert trigger_body["outcome"] == "ok", (
            f"expected outcome 'ok', got {trigger_body['outcome']!r}; check the "
            "reservations and ai-orchestrator service logs for action=purpose_classify_* "
            "and confirm AI_PURPOSE_CLASSIFICATION_ENABLED is set on the ai-orchestrator "
            "container"
        )
        assert trigger_body["purpose_suggestion"]["top_category"]

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
            f"{POLL_TIMEOUT_SECONDS}s of a 200 'ok' trigger response, a read-after-write "
            "gap across the gateway wider than expected"
        )
        assert found["purpose_suggestion"]["top_category"]
        assert found["purpose_category"] is None
    finally:
        # Best-effort: the reservation is already CANCELLED (terminal), so
        # this is just cleanup of the admin review queue, not a state check.
        await admin_client.post(f"/reservations/admin/purpose-review/{reservation_id}/dismiss")


async def test_trigger_not_eligible_before_terminal(admin_client, fresh_device):
    """A reservation that has not reached a terminal state yet (PENDING,
    since it starts in the future) has no purpose_classify_requested_at, so
    the trigger 409s not_eligible (issue #808). Does not need AI configured:
    the eligibility check runs before any call to the orchestrator.
    """
    create = await admin_client.post(
        "/reservations/", json=_reservation_body(fresh_device["id"], future=True)
    )
    assert create.status_code == 201, create.text
    reservation_id = create.json()["id"]

    try:
        resp = await admin_client.post(
            f"/reservations/admin/purpose-review/{reservation_id}/classify"
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == {"error": "not_eligible"}
    finally:
        await admin_client.delete(f"/reservations/{reservation_id}")


async def test_trigger_is_admin_only(user_client):
    resp = await user_client.post(f"/reservations/admin/purpose-review/{uuid.uuid4()}/classify")
    assert resp.status_code == 403


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
