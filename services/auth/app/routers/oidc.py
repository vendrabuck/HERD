"""OpenID Connect single sign-on routes (ADR 0016 phase 3, issue #37).

Three browser-facing routes join the phase 1 protocol module (`oidc_service`)
and the phase 2 flow store (`oidc_flow_service`):

- `GET /oidc/login` begins a flow: it builds the authorization request, stores
  a `pending` flow row with the raw `next` (the flow store sanitizes it), sets
  the `herd_oidc_flow` binding cookie, and answers 302 to the identity
  provider. Any failure answers 302 to `/login?sso_error=unavailable`.
- `GET /oidc/callback` claims the flow row (state plus binding cookie),
  redeems the code with the claimed row's verifier and nonce, resolves or
  provisions the account, completes the row with a handoff secret, sets the
  `herd_oidc_handoff` cookie, and answers 302 to `/login/sso`. Every answer
  is a 302 that clears `herd_oidc_flow`; a failure after the claim moves the
  row to `failed`. The `Location` follows ADR 0016's callback outcome table.
- `POST /oidc/exchange` consumes the handoff cookie once and answers the
  `POST /login` token body plus `next`.

Dark: outside `auth_method == "oidc"` (which `Settings` does not admit until
ADR 0016 phase 6) each route answers 404 `Single sign-on is not enabled`
before FastAPI reads its query, cookies, or body, and opens no database
session (`_SingleSignOnRoute`).

Cookies (ADR 0016, Cookies): both HttpOnly, Secure, host-only (no Domain), and
scoped to `oidc_cookie_path` (the path of OIDC_REDIRECT_URI minus `/callback`,
normally `/api/auth/oidc`; `DEFAULT_COOKIE_PATH` when the redirect URI is
unusable, the case where no begin can have set one). `herd_oidc_flow` is
SameSite=Lax, Max-Age 600; `herd_oidc_handoff` is SameSite=Strict, Max-Age 60.
"Cleared" is Starlette's `delete_cookie`: the same name, Path, and attributes,
the value `""`, Max-Age 0, and an expiry of now.

Log rule (ADR 0016 D9): nothing here logs a secret (the binding or handoff
secret, `state`, `code`, `nonce`, the verifier, the authorization URL, any
token) or anything the provider sent (the `error` and `error_description`
parameters, the `iss` parameter, a claim, a `VerifiedIdToken`). Refusals log
a fixed `reason`; the flow id is the correlation key. The catch-all around the
login and callback bodies logs the exception's class name only, never
`exc_info` and never its message, and answers `unavailable`.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import oidc_cookie_path, settings
from app.database import get_db
from app.schemas.auth import FULL_NAME_MAX_LENGTH, OidcExchangeRequest, OidcExchangeResponse
from app.services import auth_service, oidc_flow_service, oidc_service
from app.services.auth_service import FederatedLogin, oidc_username
from app.services.oidc_service import (
    OidcRejectedError,
    OidcUnavailableError,
    VerifiedIdToken,
)

logger = logging.getLogger(__name__)

FLOW_COOKIE = "herd_oidc_flow"
HANDOFF_COOKIE = "herd_oidc_handoff"
FLOW_COOKIE_MAX_AGE = 600
HANDOFF_COOKIE_MAX_AGE = 60
# 32 random bytes, base64url: 43 characters (ADR 0016, Cookies).
SECRET_BYTES = 32
DEFAULT_COOKIE_PATH = "/api/auth/oidc"

NOT_ENABLED_DETAIL = "Single sign-on is not enabled"
EXCHANGE_REFUSED_DETAIL = "Single sign-on session expired; sign in again"

SUCCESS_LOCATION = "/login/sso"
SSO_CANCELLED = "cancelled"
SSO_UNAVAILABLE = "unavailable"
SSO_FAILED = "failed"

NO_STORE = {"Cache-Control": "no-store"}


def error_location(code: str) -> str:
    return f"/login?sso_error={code}"


class _SingleSignOnRoute(APIRoute):
    """A route that answers 404 outside OIDC mode before anything else runs.

    The check wraps FastAPI's own request handler, so outside OIDC mode no
    query parameter, cookie, or body is parsed or validated (a malformed body
    gets the 404 too, never a 422) and no dependency, `get_db` included, runs.
    """

    def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
        handler = super().get_route_handler()

        async def route_handler(request: Request) -> Response:
            if settings.auth_method != "oidc":
                return JSONResponse(
                    {"detail": NOT_ENABLED_DETAIL}, status_code=404, headers=dict(NO_STORE)
                )
            return await handler(request)

        return route_handler


router = APIRouter(prefix="/oidc", tags=["oidc"], route_class=_SingleSignOnRoute)


# --- helpers ---


def _cookie_path() -> str:
    return oidc_cookie_path(settings) or DEFAULT_COOKIE_PATH


def _redirect(location: str) -> RedirectResponse:
    return RedirectResponse(location, status_code=302, headers=dict(NO_STORE))


def _set_cookie(response: Response, name: str, value: str, *, max_age: int, samesite: str) -> None:
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        path=_cookie_path(),
        secure=True,
        httponly=True,
        samesite=samesite,
    )


def _clear_cookie(response: Response, name: str, *, samesite: str) -> None:
    response.delete_cookie(name, path=_cookie_path(), secure=True, httponly=True, samesite=samesite)


def _log_refusal(message: str, reason: str, flow_id: uuid.UUID | None = None) -> None:
    extra = {"action": "oidc_callback_refused", "reason": reason}
    if flow_id is not None:
        extra["flow_id"] = str(flow_id)
    logger.warning(message, extra=extra)


def _log_unexpected(step: str, exc: Exception) -> None:
    """Class name only: an exception's message can carry a secret or provider
    text, and a traceback can carry local values."""
    logger.error(
        "OIDC %s failed unexpectedly",
        step,
        extra={"action": "oidc_internal_error", "step": step, "error_class": type(exc).__name__},
    )


def federated_login_from_token(verified: VerifiedIdToken) -> FederatedLogin:
    """Map a verified ID token onto provisioning's input (ADR 0016, Internal
    interfaces): the username by option C (`oidc_username`: the username claim
    when it fits `OIDC_USERNAME_PATTERN`, else the email when it does, else
    None, which a first login refuses), and `full_name` as the `name` claim cut
    to 200 characters, or the username."""
    username = oidc_username(verified.username_claim, verified.email)
    full_name = verified.name[:FULL_NAME_MAX_LENGTH] if verified.name else username
    return FederatedLogin(
        issuer=verified.issuer,
        subject=verified.subject,
        email=verified.email,
        email_verified=verified.email_verified,
        username=username,
        full_name=full_name,
    )


# --- GET /oidc/login ---


@router.get("/login", status_code=302, response_class=RedirectResponse)
async def oidc_login(
    next_path: str | None = Query(
        default=None,
        alias="next",
        description=(
            "Same-origin path to land on after sign-in; anything unsafe becomes "
            "/topology, never an error"
        ),
    ),
    db: AsyncSession = Depends(get_db),
):
    """Begin a sign-in: 302 to the identity provider, setting `herd_oidc_flow`.

    Answers 302 to `/login?sso_error=unavailable`, setting no cookie, when the
    settings are incomplete, discovery cannot be fetched or validated, the flow
    store refuses the secrets (a `ValueError`), or anything else raises.
    """
    try:
        authorization = await oidc_service.build_authorization_request()
        binding_secret = secrets.token_urlsafe(SECRET_BYTES)
        flow_id = await oidc_flow_service.start_flow(
            db,
            state=authorization.state,
            nonce=authorization.nonce,
            code_verifier=authorization.code_verifier,
            binding_secret=binding_secret,
            next_path=next_path,
        )
    except OidcUnavailableError:
        # oidc_service has logged the fixed reason.
        return _redirect(error_location(SSO_UNAVAILABLE))
    except ValueError:
        logger.warning(
            "OIDC sign-in not started: the flow store refused the request",
            extra={"action": "oidc_login_refused", "reason": "flow_store_refused"},
        )
        return _redirect(error_location(SSO_UNAVAILABLE))
    except Exception as exc:
        _log_unexpected("login", exc)
        return _redirect(error_location(SSO_UNAVAILABLE))

    response = _redirect(authorization.url)
    _set_cookie(response, FLOW_COOKIE, binding_secret, max_age=FLOW_COOKIE_MAX_AGE, samesite="lax")
    logger.info(
        "OIDC sign-in started", extra={"action": "oidc_login_started", "flow_id": str(flow_id)}
    )
    return response


# --- GET /oidc/callback ---


async def _fail(db: AsyncSession, flow_id: uuid.UUID) -> None:
    """Move a claimed row to failed; a failure here is logged by class name and
    never changes the answer the caller has already chosen."""
    try:
        await db.rollback()
        await oidc_flow_service.fail_flow(db, flow_id)
    except Exception as exc:
        _log_unexpected("fail_flow", exc)


async def _finish_claimed(
    db: AsyncSession,
    claimed: oidc_flow_service.ClaimedFlow,
    *,
    code: str | None,
    error: str | None,
    iss: str | None,
) -> tuple[str, str | None]:
    """Everything after a successful claim: (Location, handoff secret or None).

    Every failure moves the row to failed before it returns. Rows of the
    outcome table, in order: an `error` parameter, no `code`, an `iss`
    parameter that differs from OIDC_ISSUER, the provider unavailable, the
    provider's answer refused, provisioning refused or the account inactive,
    and the completion's guarded update not matching.
    """
    if error is not None:
        cancelled = error == "access_denied"
        _log_refusal(
            "OIDC callback carried an error from the identity provider",
            "idp_access_denied" if cancelled else "idp_error",
            claimed.id,
        )
        await _fail(db, claimed.id)
        return error_location(SSO_CANCELLED if cancelled else SSO_FAILED), None
    if not code:
        _log_refusal("OIDC callback carried no code", "missing_code", claimed.id)
        await _fail(db, claimed.id)
        return error_location(SSO_FAILED), None
    # RFC 9207: when the provider names itself, it must be the configured issuer.
    if iss is not None and iss != settings.oidc_issuer:
        _log_refusal("OIDC callback named another issuer", "issuer_parameter_mismatch", claimed.id)
        await _fail(db, claimed.id)
        return error_location(SSO_FAILED), None

    try:
        verified = await oidc_service.redeem_code(code, claimed.code_verifier, claimed.nonce)
    except OidcUnavailableError:
        await _fail(db, claimed.id)
        return error_location(SSO_UNAVAILABLE), None
    except OidcRejectedError:
        await _fail(db, claimed.id)
        return error_location(SSO_FAILED), None

    user = await auth_service.resolve_federated_user(db, federated_login_from_token(verified))
    if user is None:
        # resolve_federated_user has logged the fixed reason.
        await _fail(db, claimed.id)
        return error_location(SSO_FAILED), None
    # Read now: complete_flow commits or rolls back, and either can expire the
    # instance, after which an attribute read would need a lazy load.
    user_id = user.id

    handoff_secret = secrets.token_urlsafe(SECRET_BYTES)
    completed = await oidc_flow_service.complete_flow(
        db, claimed.id, user_id=user_id, handoff_secret=handoff_secret
    )
    if not completed:
        _log_refusal("OIDC flow could not be completed", "completion_refused", claimed.id)
        await _fail(db, claimed.id)
        return error_location(SSO_FAILED), None
    logger.info(
        "OIDC sign-in authenticated",
        extra={
            "action": "oidc_callback_authenticated",
            "flow_id": str(claimed.id),
            "user_id": str(user_id),
        },
    )
    return SUCCESS_LOCATION, handoff_secret


@router.get("/callback", status_code=302, response_class=RedirectResponse)
async def oidc_callback(
    request: Request,
    state: str | None = None,
    code: str | None = None,
    error: str | None = None,
    iss: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """The identity provider's redirect back: always a 302 clearing `herd_oidc_flow`.

    `Location` is `/login/sso` (with `herd_oidc_handoff` set) on success, else
    `/login?sso_error=` `cancelled`, `unavailable`, or `failed` per ADR 0016's
    callback outcome table; other query parameters, `error_description`
    included, are ignored and never logged.
    """
    binding_secret = request.cookies.get(FLOW_COOKIE)
    claimed_id: uuid.UUID | None = None
    handoff_secret: str | None = None
    try:
        if not state:
            _log_refusal("OIDC callback refused", "missing_state")
            location = error_location(SSO_FAILED)
        elif not binding_secret:
            _log_refusal("OIDC callback refused", "missing_flow_cookie")
            location = error_location(SSO_FAILED)
        else:
            claimed = await oidc_flow_service.claim_flow(
                db, state=state, binding_secret=binding_secret
            )
            if claimed is None:
                # claim_flow has logged the fixed reason.
                location = error_location(SSO_FAILED)
            else:
                claimed_id = claimed.id
                location, handoff_secret = await _finish_claimed(
                    db, claimed, code=code, error=error, iss=iss
                )
    except Exception as exc:
        _log_unexpected("callback", exc)
        if claimed_id is not None:
            await _fail(db, claimed_id)
        location, handoff_secret = error_location(SSO_UNAVAILABLE), None

    response = _redirect(location)
    _clear_cookie(response, FLOW_COOKIE, samesite="lax")
    if handoff_secret is not None:
        _set_cookie(
            response,
            HANDOFF_COOKIE,
            handoff_secret,
            max_age=HANDOFF_COOKIE_MAX_AGE,
            samesite="strict",
        )
    return response


# --- POST /oidc/exchange ---


def _exchange_refused() -> JSONResponse:
    response = JSONResponse(
        {"detail": EXCHANGE_REFUSED_DETAIL}, status_code=401, headers=dict(NO_STORE)
    )
    _clear_cookie(response, HANDOFF_COOKIE, samesite="strict")
    return response


@router.post(
    "/exchange",
    response_model=OidcExchangeResponse,
    responses={401: {"description": EXCHANGE_REFUSED_DETAIL}},
)
async def oidc_exchange(
    request: Request,
    body: OidcExchangeRequest,
    db: AsyncSession = Depends(get_db),
):
    """Trade the one-time handoff cookie for HERD tokens, once.

    The body must be exactly `{}` sent as JSON, else FastAPI's default 422.
    FastAPI's strict content type (on by default) reads a body as JSON only
    when Content-Type is `application/json` or another `application/*+json`;
    with none, or any other, the body stays bytes and the model refuses it.
    A foreign origin can send none of those without a CORS preflight, which
    auth's CORS policy refuses (ADR 0016 D3).
    401 `Single sign-on session expired; sign in again` when the cookie is
    missing, unknown, already exchanged, expired, or its user is inactive, the
    last checked twice: in the exchange's own guarded update and again on the
    reloaded account just before tokens are issued. The 200 and the 401 both
    clear the cookie.
    """
    handoff_secret = request.cookies.get(HANDOFF_COOKIE)
    if not handoff_secret:
        logger.warning(
            "OIDC handoff exchange refused",
            extra={"action": "oidc_exchange_refused", "reason": "missing_handoff_cookie"},
        )
        return _exchange_refused()
    exchanged = await oidc_flow_service.exchange_handoff(db, handoff_secret=handoff_secret)
    if exchanged is None:
        # exchange_handoff has logged the fixed reason.
        return _exchange_refused()
    user_id, next_path = exchanged
    user = await auth_service.get_user_by_id(db, user_id)
    if user is None or not user.is_active:
        logger.warning(
            "OIDC handoff exchange refused",
            extra={
                "action": "oidc_exchange_refused",
                "reason": "user_inactive_after_exchange",
                "user_id": str(user_id),
            },
        )
        return _exchange_refused()
    access_token, refresh_token = await auth_service.create_tokens_for_user(db, user)
    logger.info(
        "OIDC sign-in completed",
        extra={"action": "login_success", "source": "oidc", "user_id": str(user_id)},
    )
    response = JSONResponse(
        OidcExchangeResponse(
            access_token=access_token, refresh_token=refresh_token, next=next_path
        ).model_dump(),
        headers=dict(NO_STORE),
    )
    _clear_cookie(response, HANDOFF_COOKIE, samesite="strict")
    return response
