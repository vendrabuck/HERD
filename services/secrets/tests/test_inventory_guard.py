"""Transport-level tests for the issue #456 inventory reference guard.

Mirrors inventory's restore-guard test approach: patch httpx.AsyncClient.get
inside the guard module and drive the 200 / transport-error / 5xx branches.
The router-level 409/503 behavior is pinned in test_api.py.
"""

import uuid
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from app.services.inventory_guard import find_hypervisors_referencing_secret
from fastapi import HTTPException

_INVENTORY_GET = "app.services.inventory_guard.httpx.AsyncClient.get"


async def test_returns_reference_list_on_200():
    refs = [{"id": str(uuid.uuid4()), "name": "Proxmox A"}]
    mock_get = AsyncMock(return_value=httpx.Response(200, json=refs))
    with patch(_INVENTORY_GET, new=mock_get):
        result = await find_hypervisors_referencing_secret(uuid.uuid4())
    assert result == refs
    # The lookup goes to inventory's internal route with the internal token.
    (url,) = mock_get.call_args.args
    assert url.endswith("/internal") and "/hypervisors/by-secret/" in url
    assert "X-Internal-Token" in mock_get.call_args.kwargs["headers"]


async def test_empty_reference_list_passes_through():
    with patch(_INVENTORY_GET, new=AsyncMock(return_value=httpx.Response(200, json=[]))):
        assert await find_hypervisors_referencing_secret(uuid.uuid4()) == []


async def test_transport_error_fails_closed_503():
    with patch(_INVENTORY_GET, new=AsyncMock(side_effect=httpx.ConnectError("boom"))):
        with pytest.raises(HTTPException) as exc_info:
            await find_hypervisors_referencing_secret(uuid.uuid4())
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == (
        "inventory service unreachable while checking secret references"
    )


async def test_upstream_error_fails_closed_503():
    with patch(_INVENTORY_GET, new=AsyncMock(return_value=httpx.Response(500))):
        with pytest.raises(HTTPException) as exc_info:
            await find_hypervisors_referencing_secret(uuid.uuid4())
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == (
        "inventory service returned an error while checking secret references"
    )


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"<html>proxy error</html>"),
        httpx.Response(200, json={"error": "gateway"}),
        httpx.Response(200, json=["hv-1", "hv-2"]),
        httpx.Response(200, json=None),
        httpx.Response(200, json=[{"id": "a", "name": "ok"}, 5]),
    ],
    ids=["non-json", "json-object", "list-of-strings", "json-null", "mixed-list"],
)
async def test_malformed_200_fails_closed_503(response):
    """Issue #1084: a 200 the guard cannot read is the same fail-closed 503 as
    a 5xx, never an unhandled 500 (JSONDecodeError or AttributeError)."""
    with patch(_INVENTORY_GET, new=AsyncMock(return_value=response)):
        with pytest.raises(HTTPException) as exc_info:
            await find_hypervisors_referencing_secret(uuid.uuid4())
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == (
        "inventory service returned an error while checking secret references"
    )
