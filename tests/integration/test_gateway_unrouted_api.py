"""An /api path no service owns answers as an API, not as the web app (issue #1320).

Traefik sends each /api/<service> prefix to its service and drops that route
while the service's container is stopped or unhealthy; the request then falls
through to the frontend's catch-all router. The frontend's nginx used to answer
it with the SPA's index.html and a 200, which the pages read as data and
crashed on. Its `location ^~ /api/` now answers 503 with a JSON detail.

A path under a prefix no service registers takes exactly the route a stopped
service's path takes (the frontend's `PathPrefix(`/`)`), so it proves the
fallback without stopping anything on the shared stack.
"""

import httpx
import pytest

UNROUTED_DETAIL = "This part of HERD is not answering right now. Try again in a moment."


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_an_unrouted_api_path_is_a_json_503_not_the_web_app(base_url, method):
    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
        resp = await client.request(method, f"{base_url}/no-such-service-1320/items")
    assert resp.status_code == 503
    assert resp.headers["content-type"].split(";")[0].strip() == "application/json"
    assert resp.json() == {"detail": UNROUTED_DETAIL}
    assert "<html" not in resp.text.lower()


@pytest.mark.asyncio
async def test_an_unrouted_api_path_ending_like_a_static_asset_is_still_the_json_503(base_url):
    # The static-asset regex location must not claim an /api path.
    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
        resp = await client.get(f"{base_url}/no-such-service-1320/logo.svg")
    assert resp.status_code == 503
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json() == {"detail": UNROUTED_DETAIL}


@pytest.mark.asyncio
async def test_a_page_route_still_serves_the_web_app(base_url):
    # The guard covers /api/ only: a client route is still the SPA.
    origin = base_url.rsplit("/api", 1)[0]
    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
        resp = await client.get(f"{origin}/admin/drivers")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
