import enum
import uuid

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.security.utils import get_authorization_scheme_param
from jose import JWTError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.user import Role, User
from app.services.auth_service import get_user_by_id
from app.utils.jwt import verify_access_token

bearer_scheme = HTTPBearer()

# Rank order for the three roles: user is least privileged, superadmin most.
_ROLE_RANK = {Role.USER: 0, Role.ADMIN: 1, Role.SUPERADMIN: 2}


class _ClaimAvailability(enum.Enum):
    """The two ways _decode_role_claim can come back with no claim string.

    These are NOT interchangeable, on purpose: only NO_HEADER is safe to
    treat as "no signal, trust the database role". Collapsing the two into
    one fallback would be fail-open by construction, silently reopening the
    bypass this whole mechanism exists to close the moment get_current_user
    is ever changed to accept credentials from somewhere other than a bearer
    Authorization header (a cookie, a query parameter): a request could then
    satisfy get_current_user while still reading as "no credentials" here.

    NO_HEADER: the request carries no Authorization header at all. On a real
    request this cannot happen for a route that reaches this far, since
    get_current_user's own bearer_scheme dependency already requires one and
    401s first; the only way to reach get_effective_role with no header
    present is a test that has replaced get_current_user with a double, and
    in that case there is no claim to compare against, so the caller's
    current database role is used as-is (see get_effective_role).

    UNDECODABLE: an Authorization header IS present but is not a decodable
    bearer token (wrong scheme, an empty token, or a token that fails to
    decode). On a real request this means get_current_user was satisfied by
    something other than this same header, which should be impossible
    today; unlike NO_HEADER, this is not a state a legitimate test double
    needs to pass through, so it fails CLOSED to user rather than trusting
    the database role.
    """

    NO_HEADER = "no_header"
    UNDECODABLE = "undecodable"


def effective_role(claim_role: str | None, db_role: Role) -> Role:
    """The role an authorization decision inside the auth service must use.

    This is the lower of the JWT's `role` claim and the account's current
    database role, ranked user < admin < superadmin: a token can never grant
    more than the account currently holds, even if the token itself was
    minted with a higher role before a demotion. A missing, empty, or
    unrecognized claim counts as `user` (fail closed).
    """
    try:
        claim = Role(claim_role)
    except ValueError:
        claim = Role.USER
    return claim if _ROLE_RANK[claim] <= _ROLE_RANK[db_role] else db_role


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = verify_access_token(credentials.credentials)
        user_id: str = payload.get("sub")
        if user_id is None:
            raise credentials_exception
        # Parse the subject inside the try: a validly-signed token can still carry
        # a non-UUID sub, and uuid.UUID() raises ValueError (not a JWTError), which
        # would otherwise propagate as a 500 instead of a 401.
        user_uuid = uuid.UUID(user_id)
    except (JWTError, ValueError):
        raise credentials_exception

    user = await get_user_by_id(db, user_uuid)
    if user is None or not user.is_active:
        raise credentials_exception
    return user


async def _decode_role_claim(request: Request) -> str | None | _ClaimAvailability:
    """Best-effort read of the `role` claim straight off the request's own
    bearer token.

    Deliberately independent of get_current_user's own credentials
    dependency (a plain Request parameter, not a FastAPI security scheme, so
    it adds no security requirement and no parameter to the OpenAPI
    contract) rather than threading the decoded payload through
    get_current_user's return value, so get_current_user keeps returning a
    bare User and every existing caller of it (the /me endpoint, the
    any-authenticated-user group routes) is untouched.

    Returns a plain string (or None, meaning the token decoded but has no
    `role` key) when a bearer token was present and decoded, or one of
    _ClaimAvailability's two members otherwise; see that enum for why they
    are kept distinct rather than folded into one fallback.
    """
    if not request.headers.get("Authorization"):
        return _ClaimAvailability.NO_HEADER
    # Same parsing HTTPBearer itself uses: get_authorization_scheme_param plus
    # a lowercase "bearer" comparison. Kept identical so this independent
    # decode can never disagree with get_current_user's own bearer_scheme
    # about what counts as a bearer token.
    scheme, token = get_authorization_scheme_param(request.headers.get("Authorization"))
    if scheme.lower() != "bearer" or not token:
        return _ClaimAvailability.UNDECODABLE
    try:
        payload = verify_access_token(token)
    except JWTError:
        return _ClaimAvailability.UNDECODABLE
    return payload.get("role")


async def get_effective_role(
    current_user: User = Depends(get_current_user),
    role_claim: str | None | _ClaimAvailability = Depends(_decode_role_claim),
) -> Role:
    """The effective role to use for every authorization decision in this service.

    See effective_role() for the rule applied to a decoded claim.
    _ClaimAvailability.NO_HEADER means there is no claim to compare against
    (only possible when get_current_user has been replaced by a test
    double), so the account's current database role is used as-is.
    _ClaimAvailability.UNDECODABLE means a header was present but could not
    be read as a bearer token at all; that should be unreachable on a real
    request (get_current_user could not have been satisfied either), so it
    fails CLOSED to user rather than trusting the database role.
    """
    if role_claim is _ClaimAvailability.NO_HEADER:
        return current_user.role
    if role_claim is _ClaimAvailability.UNDECODABLE:
        return Role.USER
    return effective_role(role_claim, current_user.role)


def require_role(*roles: Role):
    """
    Dependency factory that restricts an endpoint to users with one of the given roles.

    The check is against the caller's EFFECTIVE role (get_effective_role), the
    lower of the JWT's role claim and the account's current database role,
    not the bare database role: an API-token exchange clamps its issued JWT
    to the lesser of the token's role and the principal's role at exchange
    time, and this is what makes that clamp actually bind inside this
    service instead of being bypassable by whatever the database row says.

    Usage:
        @router.post("/admin-only")
        async def endpoint(_: User = Depends(require_role(Role.ADMIN, Role.SUPERADMIN))):
            ...
    """

    async def _check(
        current_user: User = Depends(get_current_user),
        caller_role: Role = Depends(get_effective_role),
    ) -> User:
        if caller_role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to perform this action",
            )
        return current_user

    return _check
