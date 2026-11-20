"""The request body limit through the gateway (issue #1466, OPS-VALID-9).

Every service refuses a body over `MAX_REQUEST_BODY_BYTES` with 413 before it parses
the body and before authentication runs. The gateway has no body limit of its own,
so what these tests see is each service's answer, carried through Traefik. Nothing
here signs in (the stack suites share one login allowance per source, IAM-LOGIN-7):
every request carries no credentials, so a 401 would mean authentication ran first
and a 422 that the body was parsed. The limit is read from the environment or the
repo-root `.env` (the conftest loads it), as the stack reads it.
"""

import os

import httpx
import pytest

pytestmark = pytest.mark.asyncio

LIMIT = int(os.getenv("MAX_REQUEST_BODY_BYTES") or 4 * 1024 * 1024)
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

# One JSON route per service, under the gateway's /api prefix.
ROUTES = [
    ("POST", "/acl/grants"),
    ("POST", "/ai/commit"),
    ("POST", "/auth/groups"),
    ("POST", "/cabling/topologies"),
    ("PUT", "/config/settings"),
    ("POST", "/execution/execute"),
    ("POST", "/inventory/devices"),
    ("PUT", "/notifications/notifications/preferences"),
    ("POST", "/reservations/"),
    ("POST", "/secrets/secrets"),
    ("PUT", "/user-profile/preferences"),
    ("POST", "/v1/webhooks"),
]


def _client(base_url: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=base_url, verify=False, timeout=60.0)


@pytest.mark.parametrize(("method", "path"), ROUTES)
async def test_a_body_over_the_limit_is_413_through_the_gateway(base_url, method, path):
    async with _client(base_url) as client:
        resp = await client.request(method, path, content=b"x" * (LIMIT + 1), headers=JSON)
    assert resp.status_code == 413, resp.text[:300]
    assert resp.json() == EXPECTED


async def test_a_chunked_body_over_the_limit_is_413_through_the_gateway(base_url):
    async def parts():
        step = 256 * 1024
        sent = 0
        while sent <= LIMIT:
            yield b" " * step
            sent += step

    async with _client(base_url) as client:
        resp = await client.post("/inventory/devices", content=parts(), headers=JSON)
    assert resp.status_code == 413, resp.text[:300]
    assert resp.json() == EXPECTED


async def test_a_body_at_the_limit_reaches_the_parser_through_the_gateway(base_url):
    """Not JSON, exactly the limit: handed on whole, so FastAPI's own 422."""
    async with _client(base_url) as client:
        resp = await client.post("/inventory/devices", content=b"x" * LIMIT, headers=JSON)
    assert resp.status_code == 422, resp.text[:300]
    assert resp.json()["detail"][0]["type"] == "json_invalid"
