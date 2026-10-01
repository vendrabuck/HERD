"""Unit tests for the admin device DELETE guard helper (issues #391 and #900).

The matrix: {member blocking yes or no} x {transit blocking yes or no} x
{reservations up or down} x {cabling up or down}, pinning the exact 409 and 503
details. The reservations side is patched at `find_blocking_reservations_for_device`;
the cabling side is exercised through `call_service` with fake responses so the
transport, status, and body failure modes are all covered.
"""

import itertools
import uuid
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from app.config import settings
from app.services import device_delete_guard as guard
from app.services.device_delete_guard import (
    UNVERIFIABLE_DETAIL,
    assert_device_deletable,
    find_fork_reservation_ids_for_device,
)
from fastapi import HTTPException

DEVICE = uuid.UUID("00000000-0000-0000-0000-0000000000d1")
MEMBER_RID = "11111111-1111-1111-1111-111111111111"
TRANSIT_RID = "22222222-2222-2222-2222-222222222222"
UNREACHABLE = "unreachable"


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setattr(settings, "internal_api_token", "tok")


def _reservations_patch(*, member: bool, up: bool):
    if not up:
        return patch.object(
            guard,
            "find_blocking_reservations_for_device",
            new=AsyncMock(side_effect=HTTPException(status_code=503, detail="reservations down")),
        )
    rows = [{"id": MEMBER_RID, "status": "ACTIVE"}] if member else []
    return patch.object(
        guard, "find_blocking_reservations_for_device", new=AsyncMock(return_value=rows)
    )


def _cabling_response(ids):
    return httpx.Response(200, json={"reservation_ids": ids})


def _cabling_patch(*, transit: bool, up: bool, member: bool = False):
    if not up:
        return patch.object(
            guard, "call_service", new=AsyncMock(side_effect=httpx.ConnectError("down"))
        )
    ids = []
    if transit:
        ids.append(TRANSIT_RID)
    if member:
        # A member whose fork also touches the device: still a member, not transit-only.
        ids.append(MEMBER_RID)
    return patch.object(guard, "call_service", new=AsyncMock(return_value=_cabling_response(ids)))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "member,transit,res_up,cab_up", list(itertools.product([False, True], repeat=4))
)
async def test_matrix(member, transit, res_up, cab_up):
    with (
        _reservations_patch(member=member, up=res_up),
        _cabling_patch(transit=transit, up=cab_up),
    ):
        if not res_up or not cab_up:
            with pytest.raises(HTTPException) as ei:
                await assert_device_deletable(DEVICE)
            assert ei.value.status_code == 503
            assert ei.value.detail == "Could not verify device is not in use"
            return
        if not member and not transit:
            assert await assert_device_deletable(DEVICE) is None
            return
        with pytest.raises(HTTPException) as ei:
            await assert_device_deletable(DEVICE)
    assert ei.value.status_code == 409
    expected_ids = sorted(([MEMBER_RID] if member else []) + ([TRANSIT_RID] if transit else []))
    assert ei.value.detail == {
        "error": "device_in_use",
        "reservation_ids": expected_ids,
        "transit_reservation_ids": [TRANSIT_RID] if transit else [],
    }


@pytest.mark.asyncio
async def test_member_whose_fork_also_touches_device_is_not_transit_only():
    with (
        _reservations_patch(member=True, up=True),
        _cabling_patch(transit=True, up=True, member=True),
    ):
        with pytest.raises(HTTPException) as ei:
            await assert_device_deletable(DEVICE)
    assert ei.value.detail == {
        "error": "device_in_use",
        "reservation_ids": sorted([MEMBER_RID, TRANSIT_RID]),
        "transit_reservation_ids": [TRANSIT_RID],
    }


@pytest.mark.asyncio
async def test_non_503_reservation_error_propagates():
    with patch.object(
        guard,
        "find_blocking_reservations_for_device",
        new=AsyncMock(side_effect=HTTPException(status_code=500, detail="boom")),
    ):
        with pytest.raises(HTTPException) as ei:
            await assert_device_deletable(DEVICE)
    assert ei.value.status_code == 500


@pytest.mark.asyncio
async def test_cabling_call_shape():
    mock = AsyncMock(return_value=_cabling_response([]))
    with patch.object(guard, "call_service", new=mock):
        assert await find_fork_reservation_ids_for_device(DEVICE) == []
    args = mock.await_args
    assert args.args[1] == "GET"
    assert args.args[2] == f"/internal/forks/by-device/{DEVICE}"
    assert args.kwargs["auth"].token == "tok"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 404, 500, 503])
async def test_cabling_non_200_is_503(status):
    with patch.object(
        guard, "call_service", new=AsyncMock(return_value=httpx.Response(status, json={}))
    ):
        with pytest.raises(HTTPException) as ei:
            await find_fork_reservation_ids_for_device(DEVICE)
    assert ei.value.status_code == 503
    assert ei.value.detail == UNVERIFIABLE_DETAIL


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json={}),
        httpx.Response(200, json={"reservation_ids": 5}),
        httpx.Response(200, json=[]),
    ],
)
async def test_cabling_unparseable_body_is_503(response):
    with patch.object(guard, "call_service", new=AsyncMock(return_value=response)):
        with pytest.raises(HTTPException) as ei:
            await find_fork_reservation_ids_for_device(DEVICE)
    assert ei.value.status_code == 503
    assert ei.value.detail == UNVERIFIABLE_DETAIL


@pytest.mark.asyncio
async def test_missing_internal_token_is_503(monkeypatch):
    monkeypatch.setattr(settings, "internal_api_token", "")
    with pytest.raises(HTTPException) as ei:
        await find_fork_reservation_ids_for_device(DEVICE)
    assert ei.value.status_code == 503
    assert ei.value.detail == UNVERIFIABLE_DETAIL
