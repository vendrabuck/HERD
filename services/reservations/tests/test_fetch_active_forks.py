"""Unit tests for `_fetch_active_forks` (issue #571 item 3).

`_fetch_active_forks` is the single paginated read the standing reconciler makes
per tick (ADR 0006 Decision 5 / ADR 0007 Decision 2). Its happy path has live
nightly coverage via tests/integration/test_reservation_fork_flow.py, but three
branches never run anywhere below 200 active forks: the multi-page continuation,
the raise_for_status failure, and the no-token early return. Exercised here
against httpx.MockTransport so no real network is used.
"""

from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
from app.services.reservation_service import _fetch_active_forks


def _forks_page(entries: list[tuple[UUID, int]], total: int) -> dict:
    return {
        "forks": [
            {"reservation_id": str(rid), "latest_fork_version": version} for rid, version in entries
        ],
        "total": total,
    }


@pytest.mark.asyncio
async def test_fetch_active_forks_pages_through_multiple_pages():
    """Two full pages (limit=200 each) plus a short final page: all combined,
    and the loop terminates once skip reaches total."""
    page1 = [(uuid4(), 1) for _ in range(200)]
    page2 = [(uuid4(), 2) for _ in range(200)]
    page3 = [(uuid4(), 3) for _ in range(37)]
    total = len(page1) + len(page2) + len(page3)
    pages = [page1, page2, page3]
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        skip = int(request.url.params["skip"])
        limit = int(request.url.params["limit"])
        calls.append({"skip": skip, "limit": limit})
        index = skip // limit
        entries = pages[index]
        return httpx.Response(200, json=_forks_page(entries, total))

    transport = httpx.MockTransport(handler)

    with (
        patch("app.services.reservation_service.settings") as mock_settings,
        patch(
            "app.services.reservation_service.httpx.AsyncClient",
            return_value=httpx.AsyncClient(transport=transport),
        ),
    ):
        mock_settings.internal_api_token = "tok"
        mock_settings.cabling_service_url = "http://cabling"
        result = await _fetch_active_forks()

    expected = [(rid, v) for rid, v in page1 + page2 + page3]
    assert result == expected
    # Exactly three page fetches: skip=0, 200, 400 (the third page's skip(600)
    # >= total(437) stops the loop after that page is read, no fourth call).
    assert [c["skip"] for c in calls] == [0, 200, 400]


@pytest.mark.asyncio
async def test_fetch_active_forks_error_status_raises():
    """A non-2xx status raises via raise_for_status rather than returning partial data."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    transport = httpx.MockTransport(handler)

    with (
        patch("app.services.reservation_service.settings") as mock_settings,
        patch(
            "app.services.reservation_service.httpx.AsyncClient",
            return_value=httpx.AsyncClient(transport=transport),
        ),
    ):
        mock_settings.internal_api_token = "tok"
        mock_settings.cabling_service_url = "http://cabling"
        with pytest.raises(httpx.HTTPStatusError):
            await _fetch_active_forks()


@pytest.mark.asyncio
async def test_fetch_active_forks_no_token_returns_empty_with_no_http_call():
    """An unset internal_api_token short-circuits to [] before any HTTP call is made."""
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=_forks_page([], 0))

    transport = httpx.MockTransport(handler)

    with (
        patch("app.services.reservation_service.settings") as mock_settings,
        patch(
            "app.services.reservation_service.httpx.AsyncClient",
            return_value=httpx.AsyncClient(transport=transport),
        ),
    ):
        mock_settings.internal_api_token = ""
        mock_settings.cabling_service_url = "http://cabling"
        result = await _fetch_active_forks()

    assert result == []
    assert called is False
