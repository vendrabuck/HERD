"""Tests for the herd-common ACL helper.

Covers the explicit-grant path, the reservation-owner free pass (iter-3
widening), and the closed-by-default behavior on every failure mode.
"""

import json
import uuid

import httpx
import pytest
from herd_common.acl import (
    user_has_manage_internal,
    user_has_manage_or_owns_active_reservation,
    user_has_manage_or_owns_active_reservation_internal,
)

ACL_URL = "http://acl"
RES_URL = "http://reservations"
INTERNAL_TOKEN = "internal-token"
USER_ID = str(uuid.uuid4())
DEVICE_ID = str(uuid.uuid4())
BEARER = "Bearer fake-jwt"


class _Resp:
    def __init__(self, status_code: int, body: dict | str | None = None):
        self.status_code = status_code
        self._body = body

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body or {}

    @property
    def text(self):
        return self._body if isinstance(self._body, str) else json.dumps(self._body or {})


def _patch_http(
    monkeypatch,
    *,
    acl_response: _Resp | None = None,
    res_response: _Resp | Exception | None = None,
    acl_exception: Exception | None = None,
    res_exception: Exception | None = None,
):
    """Install a fake httpx.AsyncClient that returns/raises per-method.

    acl.py now issues both calls through herd_common.internal_client.call_service,
    which always calls client.request(method, url, ...): POST for the ACL
    check, GET for the reservations active-reservation check.
    """

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def request(self, method, url, **kwargs):
            if method == "POST":
                if acl_exception is not None:
                    raise acl_exception
                return acl_response
            if res_exception is not None:
                raise res_exception
            return res_response

    monkeypatch.setattr("herd_common.internal_client.httpx.AsyncClient", FakeClient)


@pytest.mark.asyncio
async def test_explicit_grant_returns_true(monkeypatch):
    """ACL service says allowed=True -> short-circuit, no reservation lookup needed."""
    _patch_http(monkeypatch, acl_response=_Resp(200, {"allowed": True}))
    result = await user_has_manage_or_owns_active_reservation(
        user_id=USER_ID,
        device_id=DEVICE_ID,
        authorization=BEARER,
        acl_service_url=ACL_URL,
        reservations_service_url=RES_URL,
        internal_api_token=INTERNAL_TOKEN,
    )
    assert result is True


@pytest.mark.asyncio
async def test_no_explicit_grant_falls_through_to_reservation_check(monkeypatch):
    """ACL says no, reservations service says yes -> True (the free pass)."""
    _patch_http(
        monkeypatch,
        acl_response=_Resp(200, {"allowed": False}),
        res_response=_Resp(200, {"owns_active": True}),
    )
    result = await user_has_manage_or_owns_active_reservation(
        user_id=USER_ID,
        device_id=DEVICE_ID,
        authorization=BEARER,
        acl_service_url=ACL_URL,
        reservations_service_url=RES_URL,
        internal_api_token=INTERNAL_TOKEN,
    )
    assert result is True


@pytest.mark.asyncio
async def test_no_grant_no_reservation_returns_false(monkeypatch):
    """Both gates closed -> False."""
    _patch_http(
        monkeypatch,
        acl_response=_Resp(200, {"allowed": False}),
        res_response=_Resp(200, {"owns_active": False}),
    )
    result = await user_has_manage_or_owns_active_reservation(
        user_id=USER_ID,
        device_id=DEVICE_ID,
        authorization=BEARER,
        acl_service_url=ACL_URL,
        reservations_service_url=RES_URL,
        internal_api_token=INTERNAL_TOKEN,
    )
    assert result is False


@pytest.mark.asyncio
async def test_no_bearer_token_skips_acl_check_and_tries_reservations(monkeypatch):
    """Without a bearer token, the ACL service call is skipped (no auth to forward)
    but the reservation-owner check (which uses INTERNAL_API_TOKEN) still runs.
    """
    _patch_http(monkeypatch, res_response=_Resp(200, {"owns_active": True}))
    result = await user_has_manage_or_owns_active_reservation(
        user_id=USER_ID,
        device_id=DEVICE_ID,
        authorization=None,
        acl_service_url=ACL_URL,
        reservations_service_url=RES_URL,
        internal_api_token=INTERNAL_TOKEN,
    )
    assert result is True


@pytest.mark.asyncio
async def test_acl_service_unreachable_still_tries_reservations(monkeypatch):
    """A network failure on the ACL check is not the end of the line."""
    _patch_http(
        monkeypatch,
        acl_exception=httpx.ConnectError("acl down"),
        res_response=_Resp(200, {"owns_active": True}),
    )
    result = await user_has_manage_or_owns_active_reservation(
        user_id=USER_ID,
        device_id=DEVICE_ID,
        authorization=BEARER,
        acl_service_url=ACL_URL,
        reservations_service_url=RES_URL,
        internal_api_token=INTERNAL_TOKEN,
    )
    assert result is True


@pytest.mark.asyncio
async def test_reservations_service_unreachable_returns_false(monkeypatch):
    """Both services down, no grant -> closed-by-default."""
    _patch_http(
        monkeypatch,
        acl_response=_Resp(200, {"allowed": False}),
        res_exception=httpx.ConnectError("reservations down"),
    )
    result = await user_has_manage_or_owns_active_reservation(
        user_id=USER_ID,
        device_id=DEVICE_ID,
        authorization=BEARER,
        acl_service_url=ACL_URL,
        reservations_service_url=RES_URL,
        internal_api_token=INTERNAL_TOKEN,
    )
    assert result is False


@pytest.mark.asyncio
async def test_acl_5xx_falls_through_to_reservations(monkeypatch):
    """Non-200 from ACL is treated as 'no grant', not an error to surface."""
    _patch_http(
        monkeypatch,
        acl_response=_Resp(500, {"error": "boom"}),
        res_response=_Resp(200, {"owns_active": True}),
    )
    result = await user_has_manage_or_owns_active_reservation(
        user_id=USER_ID,
        device_id=DEVICE_ID,
        authorization=BEARER,
        acl_service_url=ACL_URL,
        reservations_service_url=RES_URL,
        internal_api_token=INTERNAL_TOKEN,
    )
    assert result is True


@pytest.mark.asyncio
async def test_malformed_reservation_response_returns_false(monkeypatch):
    """ValueError on json() (non-JSON body) -> False."""
    _patch_http(
        monkeypatch,
        acl_response=_Resp(200, {"allowed": False}),
        res_response=_Resp(200, "not json"),
    )
    result = await user_has_manage_or_owns_active_reservation(
        user_id=USER_ID,
        device_id=DEVICE_ID,
        authorization=BEARER,
        acl_service_url=ACL_URL,
        reservations_service_url=RES_URL,
        internal_api_token=INTERNAL_TOKEN,
    )
    assert result is False


@pytest.mark.asyncio
async def test_malformed_acl_response_falls_through_to_reservations(monkeypatch):
    """ValueError on the ACL response json() (non-JSON body) is treated as
    'no explicit grant' (acl.py lines 51-52), so we still try the reservation
    free pass rather than erroring."""
    _patch_http(
        monkeypatch,
        acl_response=_Resp(200, "not json"),
        res_response=_Resp(200, {"owns_active": True}),
    )
    result = await user_has_manage_or_owns_active_reservation(
        user_id=USER_ID,
        device_id=DEVICE_ID,
        authorization=BEARER,
        acl_service_url=ACL_URL,
        reservations_service_url=RES_URL,
        internal_api_token=INTERNAL_TOKEN,
    )
    assert result is True


@pytest.mark.asyncio
async def test_reservations_non_200_returns_false(monkeypatch):
    """Non-200 from the reservations /internal/active endpoint (acl.py line 80)
    is treated as 'does not own', so with no ACL grant the result is False."""
    _patch_http(
        monkeypatch,
        acl_response=_Resp(200, {"allowed": False}),
        res_response=_Resp(503, {"error": "unavailable"}),
    )
    result = await user_has_manage_or_owns_active_reservation(
        user_id=USER_ID,
        device_id=DEVICE_ID,
        authorization=BEARER,
        acl_service_url=ACL_URL,
        reservations_service_url=RES_URL,
        internal_api_token=INTERNAL_TOKEN,
    )
    assert result is False


@pytest.mark.asyncio
async def test_no_internal_token_skips_reservation_lookup(monkeypatch):
    """No internal token configured -> we cannot call the reservations endpoint,
    so the reservation-owner pass returns False."""
    _patch_http(monkeypatch, acl_response=_Resp(200, {"allowed": False}))
    result = await user_has_manage_or_owns_active_reservation(
        user_id=USER_ID,
        device_id=DEVICE_ID,
        authorization=BEARER,
        acl_service_url=ACL_URL,
        reservations_service_url=RES_URL,
        internal_api_token="",
    )
    assert result is False


# --- user_has_manage_internal / *_internal (issue #704, no user JWT) --------


@pytest.mark.asyncio
async def test_manage_internal_allowed(monkeypatch):
    """ACL's internal-token route says allowed=True -> True."""
    _patch_http(monkeypatch, acl_response=_Resp(200, {"allowed": True}))
    result = await user_has_manage_internal(
        user_id=USER_ID,
        device_id=DEVICE_ID,
        acl_service_url=ACL_URL,
        internal_api_token=INTERNAL_TOKEN,
    )
    assert result is True


@pytest.mark.asyncio
async def test_manage_internal_denied(monkeypatch):
    _patch_http(monkeypatch, acl_response=_Resp(200, {"allowed": False}))
    result = await user_has_manage_internal(
        user_id=USER_ID,
        device_id=DEVICE_ID,
        acl_service_url=ACL_URL,
        internal_api_token=INTERNAL_TOKEN,
    )
    assert result is False


@pytest.mark.asyncio
async def test_manage_internal_transport_failure_returns_false(monkeypatch):
    _patch_http(monkeypatch, acl_exception=httpx.ConnectError("acl down"))
    result = await user_has_manage_internal(
        user_id=USER_ID,
        device_id=DEVICE_ID,
        acl_service_url=ACL_URL,
        internal_api_token=INTERNAL_TOKEN,
    )
    assert result is False


@pytest.mark.asyncio
async def test_manage_internal_non_200_returns_false(monkeypatch):
    _patch_http(monkeypatch, acl_response=_Resp(503, {"error": "boom"}))
    result = await user_has_manage_internal(
        user_id=USER_ID,
        device_id=DEVICE_ID,
        acl_service_url=ACL_URL,
        internal_api_token=INTERNAL_TOKEN,
    )
    assert result is False


@pytest.mark.asyncio
async def test_manage_internal_no_token_returns_false_without_calling(monkeypatch):
    """No internal token configured -> cannot call ACL, so False without a
    network attempt (closed-by-default, mirrors the reservation-owner leg)."""

    class _NeverCalled:
        def __init__(self, *a, **kw):
            raise AssertionError("must not attempt an HTTP call with no internal token")

    monkeypatch.setattr("herd_common.internal_client.httpx.AsyncClient", _NeverCalled)
    result = await user_has_manage_internal(
        user_id=USER_ID,
        device_id=DEVICE_ID,
        acl_service_url=ACL_URL,
        internal_api_token="",
    )
    assert result is False


@pytest.mark.asyncio
async def test_manage_or_reservation_internal_true_on_explicit_manage(monkeypatch):
    """Manage grant true short-circuits; the OR is satisfied without needing
    the reservation leg to also say yes."""
    _patch_http(
        monkeypatch,
        acl_response=_Resp(200, {"allowed": True}),
        res_response=_Resp(200, {"owns_active": False}),
    )
    result = await user_has_manage_or_owns_active_reservation_internal(
        user_id=USER_ID,
        device_id=DEVICE_ID,
        acl_service_url=ACL_URL,
        reservations_service_url=RES_URL,
        internal_api_token=INTERNAL_TOKEN,
    )
    assert result is True


@pytest.mark.asyncio
async def test_manage_or_reservation_internal_true_via_reservation_fallback(monkeypatch):
    """No explicit manage grant, but the reservation-owner leg says yes."""
    _patch_http(
        monkeypatch,
        acl_response=_Resp(200, {"allowed": False}),
        res_response=_Resp(200, {"owns_active": True}),
    )
    result = await user_has_manage_or_owns_active_reservation_internal(
        user_id=USER_ID,
        device_id=DEVICE_ID,
        acl_service_url=ACL_URL,
        reservations_service_url=RES_URL,
        internal_api_token=INTERNAL_TOKEN,
    )
    assert result is True


@pytest.mark.asyncio
async def test_manage_or_reservation_internal_false_when_both_deny(monkeypatch):
    _patch_http(
        monkeypatch,
        acl_response=_Resp(200, {"allowed": False}),
        res_response=_Resp(200, {"owns_active": False}),
    )
    result = await user_has_manage_or_owns_active_reservation_internal(
        user_id=USER_ID,
        device_id=DEVICE_ID,
        acl_service_url=ACL_URL,
        reservations_service_url=RES_URL,
        internal_api_token=INTERNAL_TOKEN,
    )
    assert result is False


@pytest.mark.asyncio
async def test_manage_or_reservation_internal_closed_when_both_unreachable(monkeypatch):
    _patch_http(
        monkeypatch,
        acl_exception=httpx.ConnectError("acl down"),
        res_exception=httpx.ConnectError("reservations down"),
    )
    result = await user_has_manage_or_owns_active_reservation_internal(
        user_id=USER_ID,
        device_id=DEVICE_ID,
        acl_service_url=ACL_URL,
        reservations_service_url=RES_URL,
        internal_api_token=INTERNAL_TOKEN,
    )
    assert result is False
