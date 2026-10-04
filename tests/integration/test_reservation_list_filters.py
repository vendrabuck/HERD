"""Reservations list search and filters through the gateway (issue #959).

Drives GET /api/reservations/ on the running stack with a reservation this test owns
(a per-run purpose token keeps the assertions to it on a used stack), and pins three
cross-service facts the unit suite cannot see: Traefik passes repeated `status`
parameters and timezone-aware timestamps through unchanged, a non-admin caller never
sees the admin's row under any filter, and the /api/v1 facade ignores the new
parameters (its list is unchanged by decision).
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.asyncio


async def test_list_filters_through_gateway(admin_client, user_client, fresh_device):
    token = f"it959-{uuid.uuid4().hex[:10]}"
    now = datetime.now(timezone.utc)
    create = await admin_client.post(
        "/reservations/",
        json={
            "device_ids": [fresh_device["id"]],
            "purpose": f"{token} Filter Check",
            "start_time": now.isoformat(),
            "end_time": (now + timedelta(hours=1)).isoformat(),
        },
    )
    assert create.status_code == 201, create.text
    res = create.json()
    rid = res["id"]
    try:

        async def ids(client, params):
            resp = await client.get("/reservations/", params=params)
            assert resp.status_code == 200, resp.text
            body = resp.json()
            got = [item["id"] for item in body["items"]]
            assert body["total"] == len(got)
            return got

        search = token.upper()
        assert await ids(admin_client, {"search": search}) == [rid]
        assert await ids(admin_client, {"search": rid[:8], "status": res["status"]}) == [rid]
        assert await ids(
            admin_client, [("search", token), ("status", "CANCELLED"), ("status", res["status"])]
        ) == [rid]
        assert await ids(admin_client, {"search": token, "status": "CANCELLED"}) == []
        assert await ids(admin_client, {"search": token, "purpose_category": "none"}) == [rid]
        # The row started at `now`, so it is current, not upcoming and not past.
        later = (now + timedelta(minutes=5)).astimezone(timezone(timedelta(hours=-7)))
        current = {"search": token, "starts_before": later.isoformat()}
        assert await ids(admin_client, {**current, "ends_after": later.isoformat()}) == [rid]
        assert await ids(admin_client, {"search": token, "starts_after": later.isoformat()}) == []
        assert await ids(admin_client, {"search": token, "all": "true"}) == [rid]

        # A non-admin never sees the admin's row, by purpose or by id.
        assert await ids(user_client, {"search": token}) == []
        assert await ids(user_client, {"search": rid}) == []
        denied = await user_client.get("/reservations/", params={"search": token, "all": "true"})
        assert denied.status_code == 403
        assert denied.json()["detail"] == "Only admins can list all reservations"

        for bad in (
            {"status": "BOGUS"},
            {"purpose_category": "not_a_category"},
            {"starts_after": "2030-01-01T00:00:00"},
        ):
            resp = await admin_client.get("/reservations/", params=bad)
            assert resp.status_code == 422, (bad, resp.text)

        # The v1 facade forwards only skip and limit: a search it does not know is
        # ignored, so the filtered and unfiltered v1 totals agree.
        plain = await admin_client.get("/v1/reservations", params={"limit": 1})
        filtered = await admin_client.get(
            "/v1/reservations", params={"limit": 1, "search": "no-such-purpose-anywhere"}
        )
        assert plain.status_code == filtered.status_code == 200
        assert filtered.json()["total"] == plain.json()["total"]
    finally:
        await admin_client.delete(f"/reservations/{rid}")
