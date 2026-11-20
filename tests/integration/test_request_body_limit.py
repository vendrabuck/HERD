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
"""

import os

import httpx
import pytest

pytestmark = pytest.mark.asyncio

LIMIT = int(os.getenv("MAX_REQUEST_BODY_BYTES") or 4 * 1024 * 1024)
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
# that takes a larger body (OPS-VALID-10).
ROUTES = [
    ("POST", "/acl/grants"),
    ("POST", "/ai/commit"),
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
