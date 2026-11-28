"""A connections page walk is complete over one bulk create (issue #1589).

Through Traefik on the stack's Postgres: `POST /cabling/connections/bulk` inserts
every row of a batch and commits once, so the whole batch shares one `created_at`
(Postgres `now()` is the transaction's start). `GET /cabling/connections` orders
by `created_at` descending and then by id ascending, so a skip/limit walk with a
page smaller than the batch returns every connection exactly once, in id order.
Without the id key the pages over the tie have no defined order, which is the
walk the frontend's `fetchAllPages` makes for `useDeviceConnections`.

Every connection here names one of two fresh devices and every list call filters
by one of them, so foreign rows on a used stack never enter an assertion. The
connections are deleted in a finally, before the devices' own teardown.
"""

import pytest

pytestmark = pytest.mark.asyncio

BATCH = 60
PAGE = 7


async def _walk(client, device_id: str) -> tuple[list[str], set[int]]:
    seen: list[str] = []
    totals: set[int] = set()
    skip = 0
    while True:
        resp = await client.get(
            "/cabling/connections", params={"device_id": device_id, "skip": skip, "limit": PAGE}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        totals.add(body["total"])
        seen += [c["id"] for c in body["items"]]
        skip += PAGE
        if skip >= body["total"]:
            return seen, totals


async def test_bulk_created_cables_page_through_once_each_in_id_order(admin_client, fresh_devices):
    device_a, device_b = await fresh_devices(2)
    created: list[str] = []
    try:
        items = [
            {
                "device_a_id": device_a["id"],
                "port_a": f"eth{i}",
                "device_b_id": device_b["id"],
                "port_b": f"eth{i}",
                "connection_type": "ethernet",
            }
            for i in range(BATCH)
        ]
        resp = await admin_client.post("/cabling/connections/bulk", json={"items": items})
        assert resp.status_code == 200, resp.text
        report = resp.json()
        # Assigned before any assert on the report, so a partial create (a rejected
        # row has a null connection_id) still reaches the cleanup below.
        created = [row["connection_id"] for row in report["rows"] if row["connection_id"]]
        assert report["created"] == BATCH, resp.text

        for device in (device_a, device_b):
            seen, totals = await _walk(admin_client, device["id"])
            assert totals == {BATCH}
            assert len(seen) == len(set(seen)) == BATCH
            assert set(seen) == set(created)
            # One transaction, one created_at: the id alone orders the batch. The
            # stack's uuid column sorts by its bytes, which is the order of the
            # lower-case hex text.
            assert seen == sorted(created)
    finally:
        for connection_id in created:
            await admin_client.delete(f"/cabling/connections/{connection_id}")
