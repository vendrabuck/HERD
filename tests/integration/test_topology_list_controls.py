"""GET /cabling/topologies list controls against the running stack (issue #958).

Through Traefik with real JWTs: `owner=mine` keys on the token's own `sub`, so
this is the level that proves the filter follows the real caller identity, and
that search, sort, and the filtered total agree with each other on the stack's
Postgres. Every topology here carries a per-run name prefix and the search term
is that prefix, so foreign rows on a used stack never enter an assertion; all
of them are deleted in a finally.
"""

import uuid

import pytest

from ._topology_teardown import delete_topology_checked

pytestmark = pytest.mark.asyncio


async def _create(client, name: str) -> dict:
    resp = await client.post("/cabling/topologies", json={"name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _list(client, **params) -> dict:
    resp = await client.get("/cabling/topologies", params={"limit": 500, **params})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_search_owner_sort_and_total_on_the_stack(admin_client, user_client):
    prefix = f"it958-{uuid.uuid4().hex[:8]}"
    created: list[tuple[object, str]] = []
    try:
        for client, suffix in (
            (user_client, "b-user"),
            (user_client, "A-user"),
            (admin_client, "c-admin"),
        ):
            topo = await _create(client, f"{prefix}-{suffix}")
            created.append((client, topo["id"]))
        user_ids = {tid for c, tid in created if c is user_client}

        # Case-insensitive search: the upper-cased prefix still matches all three.
        body = await _list(user_client, search=prefix.upper(), sort_by="name", sort_dir="asc")
        assert body["total"] == 3
        assert [t["name"] for t in body["items"]] == [
            f"{prefix}-A-user",
            f"{prefix}-b-user",
            f"{prefix}-c-admin",
        ]

        body = await _list(user_client, search=prefix, sort_by="name", sort_dir="desc")
        assert [t["name"] for t in body["items"]] == [
            f"{prefix}-c-admin",
            f"{prefix}-b-user",
            f"{prefix}-A-user",
        ]

        # owner=mine follows the JWT sub of each caller.
        mine = await _list(user_client, search=prefix, owner="mine")
        assert {t["id"] for t in mine["items"]} == user_ids
        assert mine["total"] == 2
        admin_mine = await _list(admin_client, search=prefix, owner="mine")
        assert [t["name"] for t in admin_mine["items"]] == [f"{prefix}-c-admin"]
        assert admin_mine["total"] == 1

        # The total is the filtered total, not the page length.
        paged = await _list(user_client, search=prefix, limit=1)
        assert len(paged["items"]) == 1
        assert paged["total"] == 3
    finally:
        for client, tid in created:
            await delete_topology_checked(client, tid)


@pytest.mark.parametrize(
    "params",
    [{"sort_by": "id"}, {"sort_dir": "sideways"}, {"owner": "theirs"}, {"search": "x" * 256}],
)
async def test_bad_values_are_422_through_the_gateway(user_client, params):
    resp = await user_client.get("/cabling/topologies", params=params)
    assert resp.status_code == 422, resp.text
