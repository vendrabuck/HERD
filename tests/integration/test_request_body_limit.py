"""The request body limit through the gateway (issue #1466, OPS-VALID-9).

Every service refuses a body over `MAX_REQUEST_BODY_BYTES` with 413 before it parses
the body and before authentication runs. The gateway has no body limit of its own,
so what these tests see is each service's answer, carried through Traefik. Nothing
here signs in (the stack suites share one login allowance per source, IAM-LOGIN-7):
every request carries no credentials, so a 401 would mean authentication ran first
and a 422 that the body was parsed. The limit is read from the environment or the
repo-root `.env` (the conftest loads it), as the stack reads it.

No attempt is retried: a connection reset in place of the 413 (what a refusal that
closed the connection caused, the #1466 review's H1) fails the test. The service
reads and discards the rest of a refused body and keeps the connection; the gateway
may still close its side of the client connection after the answer (Go's server does
when the handler leaves more than 256 KiB of a body unread, after a delay that lets
the client read the answer), so a request after a refusal must succeed, on whatever
connection the client then uses.

A route that legitimately takes a large JSON body (a canvas save, issue #1484) takes
the limit plus `LARGE_JSON_BODY_BYTES`: one test signs in once as the admin (the
session fixture every stack suite shares) and saves a canvas of exactly that limit,
then one byte more; another sends such a route a body one byte over the default
without signing in and sees authentication answer, not the 413.
"""

import json
import os
import uuid

import httpx
import pytest

from ._topology_teardown import delete_topology_checked

pytestmark = pytest.mark.asyncio

LIMIT = int(os.getenv("MAX_REQUEST_BODY_BYTES") or 256 * 1024)
# herd_common.validation_errors.LARGE_JSON_BODY_BYTES, what a large-JSON route adds.
LARGE_JSON_BODY_BYTES = 4 * 1024 * 1024
LARGE_LIMIT = LIMIT + LARGE_JSON_BODY_BYTES
ATTEMPTS = 10
JSON = {"content-type": "application/json"}
EXPECTED = {
    "detail": [
        {
            "type": "body_too_large",
            "loc": ["body"],
            "msg": f"Request body is larger than {LIMIT} bytes",
        }
    ]
}

# One JSON route per service, under the gateway's /api prefix, none of them a route
# that takes a larger body (OPS-VALID-10; the AI commit does since issue #1484).
ROUTES = [
    ("POST", "/acl/grants"),
    ("POST", "/ai/templates/suggest-identity"),
    ("POST", "/auth/groups"),
    ("POST", "/cabling/topologies"),
    ("PUT", "/config/settings"),
    ("POST", "/execution/device-check"),
    ("POST", "/inventory/devices"),
    ("PUT", "/notifications/notifications/preferences"),
    ("PATCH", "/reservations/00000000-0000-0000-0000-000000000000"),
    ("POST", "/secrets/secrets"),
    ("PUT", "/user-profile/preferences"),
    ("POST", "/v1/webhooks"),
]


def _client(base_url: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=base_url, verify=False, timeout=60.0)


async def _chunks(total: int, size: int = 256 * 1024):
    chunk = b" " * size
    sent = 0
    while sent < total:
        yield chunk
        sent += size


@pytest.mark.parametrize(("method", "path"), ROUTES)
async def test_a_body_over_the_limit_is_413_through_the_gateway(base_url, method, path):
    async with _client(base_url) as client:
        resp = await client.request(method, path, content=b"x" * (LIMIT + 1), headers=JSON)
    assert resp.status_code == 413, resp.text[:300]
    assert resp.json() == EXPECTED


async def test_a_chunked_body_over_the_limit_is_413_through_the_gateway(base_url):
    async with _client(base_url) as client:
        resp = await client.post("/inventory/devices", content=_chunks(LIMIT + 1), headers=JSON)
    assert resp.status_code == 413, resp.text[:300]
    assert resp.json() == EXPECTED


async def test_a_body_at_the_limit_reaches_the_parser_through_the_gateway(base_url):
    """Not JSON, exactly the limit: handed on whole, so FastAPI's own 422."""
    async with _client(base_url) as client:
        resp = await client.post("/inventory/devices", content=b"x" * LIMIT, headers=JSON)
    assert resp.status_code == 422, resp.text[:300]
    assert resp.json()["detail"][0]["type"] == "json_invalid"


@pytest.mark.parametrize("chunked", [False, True], ids=["declared", "chunked"])
async def test_the_connection_serves_a_request_after_each_refusal(base_url, chunked):
    """Every one of ATTEMPTS refusals arrives as the 413, then a plain request on the
    same client succeeds."""
    async with _client(base_url) as client:
        for attempt in range(ATTEMPTS):
            body = _chunks(LIMIT + 1) if chunked else b"x" * (LIMIT + 1)
            refused = await client.post("/inventory/devices", content=body, headers=JSON)
            assert refused.status_code == 413, (attempt, refused.text[:300])
            assert refused.json() == EXPECTED, attempt
            health = await client.get("/inventory/health")
            assert health.status_code == 200, (attempt, health.text[:300])


def _expected(limit: int) -> dict:
    return {
        "detail": [
            {
                "type": "body_too_large",
                "loc": ["body"],
                "msg": f"Request body is larger than {limit} bytes",
            }
        ]
    }


def _canvas_body(size: int) -> bytes:
    """A canvas PUT body of exactly `size` bytes: one network element node whose
    descriptive `attrs` carry the padding (cabling stores a canvas as it is sent, but
    for device nodes, which this canvas has none of)."""
    canvas = {
        "nodes": [
            {
                "id": "n-element",
                "type": "networkElementNode",
                "position": {"x": 100, "y": 100},
                "data": {
                    "element": {
                        "id": str(uuid.uuid4()),
                        "element_type": "vlan_segment",
                        "label": "body limit",
                        "attrs": {"description": ""},
                    }
                },
            }
        ],
        "edges": [],
    }
    empty = len(json.dumps({"canvas_data": canvas}, separators=(",", ":")))
    canvas["nodes"][0]["data"]["element"]["attrs"]["description"] = "d" * (size - empty)
    body = json.dumps({"canvas_data": canvas}, separators=(",", ":")).encode()
    assert len(body) == size
    return body


async def test_a_canvas_save_at_the_large_json_limit_succeeds_and_one_byte_more_is_413(
    admin_client,
):
    """Through the gateway, signed in: a canvas of exactly the default plus the
    large-JSON allowance is saved and read back; one byte more is the 413 naming that
    limit. The topology is deleted through the checked teardown helper whatever happens."""
    created = await admin_client.post(
        "/cabling/topologies", json={"name": f"body-limit-{uuid.uuid4().hex[:8]}"}
    )
    assert created.status_code == 201, created.text[:300]
    topology_id = created.json()["id"]
    try:
        at = await admin_client.put(
            f"/cabling/topologies/{topology_id}", content=_canvas_body(LARGE_LIMIT), headers=JSON
        )
        assert at.status_code == 200, at.text[:300]
        saved = await admin_client.get(f"/cabling/topologies/{topology_id}")
        attrs = saved.json()["canvas_data"]["nodes"][0]["data"]["element"]["attrs"]
        assert len(attrs["description"]) > LIMIT
        over = await admin_client.put(
            f"/cabling/topologies/{topology_id}",
            content=_canvas_body(LARGE_LIMIT + 1),
            headers=JSON,
        )
        assert over.status_code == 413, over.text[:300]
        assert over.json() == _expected(LARGE_LIMIT)
    finally:
        await delete_topology_checked(admin_client, topology_id)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("PUT", "/cabling/topologies/00000000-0000-0000-0000-000000000000"),
        ("POST", "/ai/commit"),
        ("POST", "/inventory/devices/00000000-0000-0000-0000-000000000000/config-versions"),
    ],
)
async def test_a_large_json_route_reads_a_body_over_the_default_to_authentication(
    base_url, method, path
):
    """No credentials: a JSON body one byte over the default is parsed by a large-JSON
    route, so its authentication answers (401 or 403), not the 413 an unmarked route
    gives the same body (the ROUTES tests above)."""
    body = b"{}" + b" " * (LIMIT - 1)
    assert len(body) == LIMIT + 1
    async with _client(base_url) as client:
        resp = await client.request(method, path, content=body, headers=JSON)
    assert resp.status_code in (401, 403), resp.text[:300]
