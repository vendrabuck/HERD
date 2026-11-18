import uuid

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security.utils import get_authorization_scheme_param
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.dependencies.auth import effective_role, get_current_user
from app.models.user import Role, User
from app.schemas.auth import (
    LoginOptionsResponse,
    LoginRequest,
    LogoutRequest,
    ProfileUpdateRequest,
    RefreshRequest,
    RegisterRequest,
    SsoOptions,
    TokenResponse,
    UserResponse,
)
from app.services import login_limits, passwords
from app.services.auth_service import (
    authenticate_user,
    create_tokens_for_user,
    create_user,
    get_user_by_email,
    get_user_by_id,
    get_user_by_username,
    revoke_refresh_token,
    rotate_refresh_token,
    update_user_profile,
)
from app.services.ldap_service import LdapUnavailableError
from app.utils.jwt import verify_access_token

router = APIRouter(tags=["auth"])

# Fixed 503 detail for an LDAP-mode login the directory could not check.
LOGIN_DIRECTORY_UNAVAILABLE_DETAIL = "Directory unavailable, credentials not checked"

REGISTER_DISABLED_LDAP_DETAIL = (
    "Local registration is disabled; this deployment uses LDAP authentication."
)
REGISTER_DISABLED_OIDC_DETAIL = (
    "Local registration is disabled; this deployment uses single sign-on."
)

# Fixed 429 detail for a source past its attempt allowance (IAM-LOGIN-7), the
# same for every account, known or unknown, and for login and registration.
ATTEMPTS_LIMITED_DETAIL = "Too many attempts; try again later"
# Fixed 503 detail when no hashing slot came free in time (IAM-HASH-7).
HASHING_BUSY_DETAIL = "Too many password checks in progress; try again shortly"

_LIMITED_RESPONSE = {
    "description": "Too many attempts from this source address (IAM-LOGIN-7); "
    "`Retry-After` gives the whole seconds to wait",
}
_REGISTER_RESPONSES: dict[int | str, dict] = {
    429: _LIMITED_RESPONSE,
    503: {"description": "Password checking is at capacity (IAM-HASH-7)"},
}
_LOGIN_RESPONSES: dict[int | str, dict] = {
    429: _LIMITED_RESPONSE,
    503: {
        "description": "Password checking is at capacity (IAM-HASH-7), or in LDAP mode "
        "the directory could not be asked (IAM-LDAP-12)",
    },
}


def _check_attempt_allowance(request: Request) -> None:
    """Count one password attempt against the request's source address; 429 with
    `Retry-After` when it has none left (IAM-LOGIN-7). Called before any query,
    bcrypt, or directory call, so a refused attempt costs nothing."""
    wait = login_limits.LIMITER.acquire(login_limits.request_source(request))
    if wait is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=ATTEMPTS_LIMITED_DETAIL,
            headers={"Retry-After": str(wait)},
        )


def _hashing_busy() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=HASHING_BUSY_DETAIL,
        headers={"Retry-After": str(passwords.HASH_RETRY_AFTER_SECONDS)},
    )


async def _registered_by_an_admin(request: Request, db: AsyncSession) -> bool:
    """True when the request carries a bearer access token of an active account
    whose effective role (IAM-ROLE-5) is admin or superadmin.

    Such a registration is not counted against the source's allowance
    (IAM-LOGIN-7): `make seed` registers about a thousand accounts this way from
    one address. Nothing else changes for it. Any other header, a token that does
    not verify, or a lower role answers False, never an error. The transaction
    the account read opened is ended here, so the connection goes back to the
    pool before the registration waits for a hashing slot.
    """
    scheme, token = get_authorization_scheme_param(request.headers.get("Authorization"))
    if scheme.lower() != "bearer" or not token:
        return False
    try:
        payload = verify_access_token(token)
        user_id = uuid.UUID(str(payload.get("sub")))
    except (jwt.PyJWTError, ValueError):
        return False
    user = await get_user_by_id(db, user_id)
    admitted = (
        user is not None
        and user.is_active
        and effective_role(payload.get("role"), user.role) in (Role.ADMIN, Role.SUPERADMIN)
    )
    await db.rollback()
    return admitted


@router.get("/login-options", response_model=LoginOptionsResponse)
async def login_options(response: Response):
    """Which sign-in methods the login page offers (ADR 0016 D10), from this
    process's live settings; public, answered in every mode, never cached.

    `password_login` is true in local and LDAP mode and, in OIDC mode, the
    value of OIDC_BREAK_GLASS_LOCAL_SUPERADMIN. `sso` is present only in OIDC
    mode, also when the OIDC settings are incomplete (the begin route then
    answers `unavailable`).
    """
    response.headers["Cache-Control"] = "no-store"
    method = settings.auth_method
    if method == "oidc":
        return LoginOptionsResponse(
            auth_method="oidc",
            registration_open=False,
            password_login=settings.oidc_break_glass_local_superadmin,
            sso=SsoOptions(provider_name=settings.oidc_provider_name),
        )
    return LoginOptionsResponse(
        auth_method=method,
        registration_open=method == "local",
        password_login=True,
        sso=None,
    )


@router.post(
    "/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    responses=_REGISTER_RESPONSES,
)
async def register(request: Request, body: RegisterRequest, db: AsyncSession = Depends(get_db)):
    if settings.auth_method == "ldap":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=REGISTER_DISABLED_LDAP_DETAIL
        )
    if settings.auth_method == "oidc":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=REGISTER_DISABLED_OIDC_DETAIL
        )
    # Registration hashes the password, so an unauthenticated one counts against
    # the source's attempt allowance (IAM-LOGIN-7), before any other query.
    if not await _registered_by_an_admin(request, db):
        _check_attempt_allowance(request)
    try:
        # The slot is entered before the duplicate check's query, so a
        # registration waiting for one holds no database connection (IAM-HASH-7).
        async with passwords.hashing_slot():
            # Single generic 409 for any collision (email OR username, pre-check
            # OR the IntegrityError race below). Distinct messages let an
            # unauthenticated caller enumerate which emails and usernames already
            # exist by reading which one comes back. Keep this wording identical
            # across all three paths.
            if await get_user_by_email(db, body.email) or await get_user_by_username(
                db, body.username
            ):
                raise HTTPException(status_code=409, detail="Email or username already exists")
            try:
                user = await create_user(
                    db, body.email, body.username, body.password, full_name=body.full_name
                )
            except IntegrityError:
                raise HTTPException(status_code=409, detail="Email or username already exists")
    except passwords.HashingBusyError:
        raise _hashing_busy()
    return user


@router.post("/login", response_model=TokenResponse, responses=_LOGIN_RESPONSES)
async def login(request: Request, body: LoginRequest, db: AsyncSession = Depends(get_db)):
    # Before anything that costs: no query, bcrypt, or directory call runs for a
    # refused attempt, and the answer is the same for every account (IAM-LOGIN-7).
    _check_attempt_allowance(request)
    try:
        user = await authenticate_user(db, body.email, body.password)
    except passwords.HashingBusyError:
        raise _hashing_busy()
    except LdapUnavailableError:
        # Issue #1403: LDAP mode could not ask the directory, so the credentials
        # were never judged; a 401 would tell the user their password is wrong.
        # The directory client's text is in the log only (auth_service and
        # ldap_service each logged it); the detail is fixed.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=LOGIN_DIRECTORY_UNAVAILABLE_DETAIL,
        )
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access_token, refresh_token = await create_tokens_for_user(db, user)
    return TokenResponse(access_token=access_token, refresh_token=refresh_token)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(body: RefreshRequest, db: AsyncSession = Depends(get_db)):
    result = await rotate_refresh_token(db, body.refresh_token)
    if not result:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        )
    access_token, new_refresh_token = result
    return TokenResponse(access_token=access_token, refresh_token=new_refresh_token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(body: LogoutRequest, db: AsyncSession = Depends(get_db)):
    await revoke_refresh_token(db, body.refresh_token)


@router.get("/me", response_model=UserResponse)
async def me(current_user: User = Depends(get_current_user)):
    return current_user


@router.patch("/me/profile", response_model=UserResponse)
async def update_my_profile(
    body: ProfileUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Edit the caller's own profile: full_name, contact_email, phone (issue #1252).

    Any signed-in active account. Login email, username, and role are not
    accepted here (the request schema forbids unknown keys).
    """
    result = await update_user_profile(
        db, current_user.id, body.changes(), modified_by=current_user.id
    )
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return result[0]
