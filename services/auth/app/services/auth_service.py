import logging
import uuid
from datetime import datetime, timezone

from passlib.context import CryptContext
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.user import RefreshToken, Role, User
from app.services import ldap_service
from app.utils.jwt import create_access_token, create_refresh_token, hash_token

logger = logging.getLogger(__name__)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Pre-computed hash used when the user is not found, so that bcrypt always runs
# and login timing does not reveal whether an email exists.
_DUMMY_HASH = pwd_context.hash("not-a-real-password")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


async def get_user_by_email(db: AsyncSession, email: str) -> User | None:
    result = await db.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


async def get_user_by_username(db: AsyncSession, username: str) -> User | None:
    result = await db.execute(select(User).where(User.username == username))
    return result.scalar_one_or_none()


async def get_user_by_id(db: AsyncSession, user_id: uuid.UUID | str) -> User | None:
    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def get_all_users(db: AsyncSession, skip: int = 0, limit: int = 50) -> tuple[list[User], int]:
    from sqlalchemy import func

    count_query = select(func.count()).select_from(User)
    total = (await db.execute(count_query)).scalar() or 0

    result = await db.execute(select(User).order_by(User.created_at).offset(skip).limit(limit))
    return list(result.scalars().all()), total


async def superadmin_exists(db: AsyncSession) -> bool:
    result = await db.execute(select(User).where(User.role == Role.SUPERADMIN))
    return result.scalar_one_or_none() is not None


async def _auto_assign_not_grouped(db: AsyncSession, user: User) -> None:
    # Captured before anything below can fail: a failed FLUSH (not just an
    # explicit rollback) already expires every instance the session tracks,
    # so username must be read now, before the try, or not safely at all.
    username = user.username
    try:
        from app.services.group_service import add_member, get_group_by_name

        not_grouped = await get_group_by_name(db, "Not Grouped")
        if not_grouped:
            await add_member(db, not_grouped.id, user.id)
    except Exception:
        # C7: a failed add here (e.g. a real commit-time IntegrityError, not
        # just a bare raise) leaves the session in SQLAlchemy's pending-
        # rollback state. Left uncleared, the NEXT operation on this session
        # raises PendingRollbackError instead of the caller's own error, and
        # in a long-lived session (the sync reconciler's, see
        # ldap_sync_service._ensure_ldap_user) that aborts the ENTIRE run
        # over one Not-Grouped assignment. The rollback is correct here for
        # the login/register path too, so it is unconditional, not gated by
        # caller.
        await db.rollback()
        # Rollback expires every instance the session tracks, not just the
        # ones touched by the failed add: it is a session-wide invalidation,
        # not a targeted one. create_user/create_ldap_user already refresh
        # user once before calling this function and hand it back to a
        # caller that expects it loaded (e.g. building a login response
        # immediately); refreshing again here restores that precondition
        # instead of leaving an expired instance as a landmine for whichever
        # caller touches it next (a bare attribute read on an expired async
        # ORM instance outside an awaited call raises MissingGreenlet, not a
        # clean reload).
        await db.refresh(user)
        logger.debug("Could not auto-assign user %s to 'Not Grouped'", username)


async def create_user(
    db: AsyncSession,
    email: str,
    username: str,
    password: str,
    role: Role = Role.USER,
) -> User:
    user = User(
        email=email,
        username=username,
        hashed_password=get_password_hash(password),
        auth_source="local",
        role=role,
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise
    await db.refresh(user)
    logger.info(
        "User registered: %s",
        username,
        extra={"action": "register", "email": email, "username": username},
    )

    await _auto_assign_not_grouped(db, user)
    return user


async def create_ldap_user(
    db: AsyncSession,
    email: str,
    username: str,
) -> User:
    user = User(
        email=email,
        username=username,
        hashed_password=None,
        auth_source="ldap",
        role=Role.USER,
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise
    await db.refresh(user)
    logger.info(
        "LDAP user provisioned: %s",
        username,
        extra={
            "action": "ldap_provision",
            "email": email,
            "username": username,
        },
    )
    await _auto_assign_not_grouped(db, user)
    return user


async def set_user_role(
    db: AsyncSession,
    target_user_id: uuid.UUID,
    new_role: Role,
    modified_by: uuid.UUID | None = None,
) -> User | None:
    user = await get_user_by_id(db, target_user_id)
    if not user:
        return None
    user.role = new_role
    if modified_by is not None:
        user.modified_by = modified_by
    await db.commit()
    await db.refresh(user)
    return user


async def set_user_active(
    db: AsyncSession, target_user_id: uuid.UUID, is_active: bool
) -> User | None:
    """Manual admin activate/deactivate (ADR 0011 phase 4, the non-sync
    writer of is_active). Always clears deactivated_by_sync: admin intent
    always outranks the directory, so a manually (de)activated user becomes
    invisible to the sweep's automatic reactivation."""
    user = await get_user_by_id(db, target_user_id)
    if not user:
        return None
    user.is_active = is_active
    user.deactivated_by_sync = False
    await db.commit()
    await db.refresh(user)
    return user


async def _authenticate_local(db: AsyncSession, email: str, password: str) -> User | None:
    user = await get_user_by_email(db, email)
    if not user:
        verify_password(password, _DUMMY_HASH)
        logger.warning(
            "Login failed: unknown email",
            extra={"action": "login_failure", "email": email},
        )
        return None
    if user.auth_source != "local" or not user.hashed_password:
        # An LDAP-sourced user must not be able to log in via the local path.
        verify_password(password, _DUMMY_HASH)
        logger.warning(
            "Login failed: wrong auth source for %s (expected local, got %s)",
            email,
            user.auth_source,
            extra={
                "action": "login_failure",
                "email": email,
                "reason": "wrong_auth_source",
            },
        )
        return None
    if not verify_password(password, user.hashed_password):
        logger.warning(
            "Login failed: wrong password for %s",
            email,
            extra={"action": "login_failure", "email": email},
        )
        return None
    if not user.is_active:
        # Mirror the LDAP path and the refresh path: a deactivated account must
        # not be issued a fresh token even with correct credentials.
        logger.warning(
            "Login failed: account deactivated for %s",
            email,
            extra={"action": "login_failure", "email": email, "reason": "inactive"},
        )
        return None
    logger.info(
        "Login success: %s",
        user.username,
        extra={"action": "login_success", "user_id": str(user.id), "email": email},
    )
    return user


async def _authenticate_ldap(db: AsyncSession, email: str, password: str) -> User | None:
    # Clients still submit `email` in the login payload. For LDAP we treat it as
    # the short-form username (sAMAccountName) that gets injected into the
    # configured search filter.
    #
    # Cross-reference (S5): ldap_sync_service._ensure_ldap_user resolves the
    # same identity shape during the reconciler's sync pass, but repairs
    # username drift and retries a provisioning race as a lookup. This
    # function deliberately does neither below: a username collision and a
    # provisioning race are both refused outright. That divergence is
    # DESIGNED, not a gap: login is a narrow, latency-sensitive path that
    # must fail closed rather than silently mutate an account mid-
    # authentication, while sync runs a long-lived pass whose job is to
    # repair drift and recover races proactively. Do not "fix" one to match
    # the other.
    identity = await ldap_service.bind_user(email, password)
    if identity is None:
        logger.warning(
            "Login failed: LDAP bind rejected",
            extra={"action": "login_failure", "email": email, "source": "ldap"},
        )
        return None

    user = await get_user_by_email(db, identity.email)
    if user is None:
        try:
            user = await create_ldap_user(db, identity.email, identity.username)
        except IntegrityError:
            # Username collision with an existing local account; refuse rather
            # than silently take over a local identity.
            logger.warning(
                "LDAP provisioning collided with existing username",
                extra={
                    "action": "login_failure",
                    "email": identity.email,
                    "username": identity.username,
                    "reason": "username_collision",
                },
            )
            return None
    elif user.auth_source != "ldap":
        logger.warning(
            "Login failed: email matches a non-LDAP account",
            extra={
                "action": "login_failure",
                "email": identity.email,
                "reason": "wrong_auth_source",
            },
        )
        return None

    if not user.is_active:
        # Mirror the local path: log the deactivated-account denial rather than
        # returning None silently, so an LDAP login refused for inactivity is
        # auditable too.
        logger.warning(
            "Login failed: account deactivated for %s",
            user.email,
            extra={
                "action": "login_failure",
                "email": user.email,
                "reason": "inactive",
                "source": "ldap",
            },
        )
        return None

    logger.info(
        "Login success (ldap): %s",
        user.username,
        extra={
            "action": "login_success",
            "user_id": str(user.id),
            "email": user.email,
            "source": "ldap",
        },
    )
    return user


async def authenticate_user(db: AsyncSession, email: str, password: str) -> User | None:
    if settings.auth_method == "ldap":
        return await _authenticate_ldap(db, email, password)
    return await _authenticate_local(db, email, password)


async def create_tokens_for_user(db: AsyncSession, user: User) -> tuple[str, str]:
    """Returns (access_token, raw_refresh_token)."""
    payload = {
        "sub": str(user.id),
        "username": user.username,
        "email": user.email,
        "role": user.role.value,
    }
    access_token = create_access_token(payload)
    raw_refresh, token_hash, expires_at = create_refresh_token()

    db_token = RefreshToken(
        user_id=user.id,
        token_hash=token_hash,
        expires_at=expires_at,
    )
    db.add(db_token)
    await db.commit()
    return access_token, raw_refresh


async def rotate_refresh_token(db: AsyncSession, raw_refresh_token: str) -> tuple[str, str] | None:
    """Validates and rotates refresh token. Returns (access_token, new_refresh_token) or None."""
    token_hash = hash_token(raw_refresh_token)
    result = await db.execute(
        select(RefreshToken).where(
            RefreshToken.token_hash == token_hash,
            RefreshToken.revoked == False,  # noqa: E712
        )
    )
    db_token = result.scalar_one_or_none()
    if not db_token:
        return None

    now = datetime.now(timezone.utc)
    expires_at = db_token.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < now:
        return None

    user = await get_user_by_id(db, db_token.user_id)
    if not user or not user.is_active:
        return None

    # Atomically consume the old token by flipping revoked False to True in a
    # single guarded UPDATE. The WHERE clause is the authoritative concurrency
    # gate; the read-and-check above is only a fast path. We re-assert all three
    # liveness predicates here so none of them can be defeated by a mutation that
    # lands in the TOCTOU window between the read above and this UPDATE:
    #   - revoked == False: a concurrent logout (revoke_refresh_token) must not be
    #     undone, else refresh resurrects a just-logged-out session.
    #   - expires_at > now: a token that expires in the window must not mint a
    #     fresh one.
    #   - user is active: a user deactivated in the window must not mint a fresh
    #     one. is_active lives on User, so it is gated via a correlated subquery.
    # If any predicate fails the UPDATE matches zero rows and we abort without
    # issuing a replacement.
    consume = await db.execute(
        update(RefreshToken)
        .where(
            RefreshToken.token_hash == token_hash,
            RefreshToken.revoked == False,  # noqa: E712
            RefreshToken.expires_at > now,
            RefreshToken.user_id.in_(
                select(User.id).where(User.id == db_token.user_id, User.is_active.is_(True))
            ),
        )
        .values(revoked=True)
    )
    if consume.rowcount == 0:
        # Lost the race to a concurrent logout (or another refresh); do not
        # resurrect the session.
        await db.rollback()
        return None
    await db.commit()

    access_token, new_raw_refresh = await create_tokens_for_user(db, user)
    return access_token, new_raw_refresh


async def revoke_refresh_token(db: AsyncSession, raw_refresh_token: str) -> bool:
    # Revocation is a single guarded UPDATE so that a logout is one atomic,
    # monotonic state transition (revoked False to True). Paired with the
    # `revoked == False` gate in rotate_refresh_token, this guarantees that a
    # concurrent refresh cannot resurrect a session a logout has revoked: whoever
    # flips the row first wins, and the loser issues nothing.
    token_hash = hash_token(raw_refresh_token)
    result = await db.execute(
        update(RefreshToken).where(RefreshToken.token_hash == token_hash).values(revoked=True)
    )
    await db.commit()
    return result.rowcount > 0
