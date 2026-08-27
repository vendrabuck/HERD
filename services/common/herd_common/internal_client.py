"""Client-side transport for one HERD service calling a sibling service.

Six places across three services hand-roll the same shape: build an HTTP
call to a sibling HERD service, attach an auth header, apply a timeout, and
translate a transport failure into that caller's own error convention. This
module is the shared transport those call sites build on; it does not
interpret the response, because the callers map failures differently on
purpose (reservations raises `RuntimeError`, inventory raises
`HTTPException` directly, the ACL helpers return `False` closed-by-default).
Interpretation stays at the call site.

`auth` selects one of two explicit variants:

    from herd_common.internal_client import InternalTokenAuth, ForwardedAuth, call_service

    # service-to-service, no acting user
    resp = await call_service(
        base_url, "GET", "/internal/admins",
        auth=InternalTokenAuth(token=settings.internal_api_token),
        timeout=5.0,
    )

    # forwards the caller's own JWT
    resp = await call_service(
        base_url, "GET", f"/groups/user/{user_id}",
        auth=ForwardedAuth(authorization=authorization),
        timeout=10.0,
    )

`InternalTokenAuth` with a missing/empty token raises `RuntimeError` (the
caller supplies the message text, since each existing call site has its own
wording); this mirrors reservations' `_cabling_fork_call` /
`_execution_wiring_call` convention. `ForwardedAuth` with a `None`
authorization is left to the caller to reject before calling `call_service`
at all (inventory's fetchers raise `HTTPException(500)` for that, a shape
this transport-only helper does not know about).

Transport errors (`httpx.HTTPError` and subclasses) propagate unchanged; a
non-2xx response is returned as-is rather than raised, so each caller keeps
its own status-code mapping.

This is the client-side counterpart to `internal_auth.internal_token_matches`,
the server-side constant-time verifier every service uses to check an inbound
`X-Internal-Token` header.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class InternalTokenAuth:
    """Service-to-service auth: an `X-Internal-Token` header, no acting user."""

    token: str | None
    missing_token_message: str = "internal_api_token not configured"


@dataclass(frozen=True)
class ForwardedAuth:
    """Forwards the caller's own bearer token as `Authorization`."""

    authorization: str


Auth = InternalTokenAuth | ForwardedAuth


async def call_service(
    base_url: str,
    method: str,
    path: str,
    *,
    json_body: dict | None = None,
    params: dict | None = None,
    timeout: float,
    auth: Auth,
) -> httpx.Response:
    """Issue one HTTP call to a sibling HERD service and return the raw response.

    `base_url` is joined with `path` verbatim (trailing/leading slashes are the
    caller's responsibility, matching every existing call site). `params`
    becomes the request's query string (e.g. inventory's device-group fetchers
    and notifications' preferences/holder lookups). Raises `RuntimeError` when
    `auth` is an `InternalTokenAuth` with no token configured, using
    `auth.missing_token_message`. Any `httpx.HTTPError` raised by the
    transport (connection failure, timeout, etc.) propagates unchanged so the
    caller applies its own mapping. A non-2xx response is returned, not
    raised; `httpx.Response.raise_for_status()` remains available to a caller
    that wants that instead.
    """
    if isinstance(auth, InternalTokenAuth):
        if not auth.token:
            raise RuntimeError(auth.missing_token_message)
        headers = {"X-Internal-Token": auth.token}
    else:
        headers = {"Authorization": auth.authorization}

    async with httpx.AsyncClient() as client:
        return await client.request(
            method,
            f"{base_url}{path}",
            headers=headers,
            params=params,
            json=json_body,
            timeout=timeout,
        )


__all__ = ["InternalTokenAuth", "ForwardedAuth", "Auth", "call_service"]
