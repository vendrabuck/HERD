import logging
import unicodedata
import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timezone

from herd_common.advisory_lock import advisory_key_from_string, xact_lock
from herd_common.pagination import paginate
from passlib.context import CryptContext
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.federated_identity import SUBJECT_MAX_LENGTH, FederatedIdentity
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


@dataclass(frozen=True)
class UserSnapshot:
    """A plain, session-independent snapshot of the User columns the LDAP
    reconciler needs to classify a member (issue #513 item 1 regression
    fix). Unlike an ORM User instance, a snapshot holds no session-bound
    state, so it can never be expired by a LATER member's rollback within
    the same batch dict: get_users_by_emails prefetches many of these
    before any is read, and the reconciler's own provisioning path
    deliberately rolls back on a race elsewhere in the same session. A
    caller that needs to MUTATE a user (the username drift repair) must
    load a fresh, live ORM instance at that point instead of reusing a
    snapshot; see ldap_sync_service._ensure_ldap_user.
    """

    id: uuid.UUID
    email: str
    username: str
    auth_source: str
    is_active: bool


# asyncpg's wire protocol caps a single prepared statement at 32767 bind
# parameters; 5000 leaves headroom for any other parameters SQLAlchemy adds
# to the same statement and keeps each chunk a reasonable size for the query
# planner regardless of driver (issue #513 item 3).
_EMAIL_LOOKUP_CHUNK_SIZE = 5000


async def get_users_by_emails(db: AsyncSession, emails: Collection[str]) -> dict[str, UserSnapshot]:
    """Batch form of get_user_by_email: one WHERE email IN (...) query per
    chunk of up to _EMAIL_LOOKUP_CHUNK_SIZE emails (issue #513 item 3),
    instead of one query per email (item 1, the LDAP reconciler's per-group
    member lookup). Returns a dict keyed by email, of plain UserSnapshot
    rows selected by COLUMN, never ORM User instances (item 1's regression
    fix: an ORM instance in this dict can be silently expired by an
    unrelated rollback elsewhere in the same session before it is read; a
    snapshot cannot, since it carries no session-bound state at all). An
    email with no matching row is simply absent from the result (mirrors
    get_user_by_email returning None for a miss). A miss still needs the
    single-email path for a provisioning miss (a create call and its
    possible IntegrityError-retry lookup, which needs a real, mutable ORM
    instance), which this function intentionally does not attempt on the
    caller's behalf.
    """
    email_list = list(emails)
    if not email_list:
        return {}
    out: dict[str, UserSnapshot] = {}
    for start in range(0, len(email_list), _EMAIL_LOOKUP_CHUNK_SIZE):
        chunk = email_list[start : start + _EMAIL_LOOKUP_CHUNK_SIZE]
        result = await db.execute(
            select(User.id, User.email, User.username, User.auth_source, User.is_active).where(
                User.email.in_(chunk)
            )
        )
        for row in result.all():
            out[row.email] = UserSnapshot(
                id=row.id,
                email=row.email,
                username=row.username,
                auth_source=row.auth_source,
                is_active=row.is_active,
            )
    return out


async def get_user_by_username(db: AsyncSession, username: str) -> User | None:
    result = await db.execute(select(User).where(User.username == username))
    return result.scalar_one_or_none()


async def get_user_by_id(db: AsyncSession, user_id: uuid.UUID | str) -> User | None:
    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def get_all_users(db: AsyncSession, skip: int = 0, limit: int = 50) -> tuple[list[User], int]:
    stmt = select(User).order_by(User.created_at)
    return await paginate(db, stmt, skip=skip, limit=limit)


async def superadmin_exists(db: AsyncSession) -> bool:
    """Whether any superadmin account exists.

    Selects the id only (issue #1261): this runs at every startup, before
    `make migrate` can, so it must never name a column a stamped schema from
    the previous release may still lack. Reading the whole User model here
    once kept auth from booting after a release added columns to users.
    """
    result = await db.execute(select(User.id).where(User.role == Role.SUPERADMIN).limit(1))
    return result.scalar_one_or_none() is not None


async def email_registered(db: AsyncSession, email: str) -> bool:
    """Whether an account with this login email exists, reading the id only.

    The startup seed's existence check (issue #1261); see superadmin_exists.
    Callers that need the account itself use get_user_by_email.
    """
    result = await db.execute(select(User.id).where(User.email == email))
    return result.scalar_one_or_none() is not None


class NotGroupedResolver:
    """Lazily resolves and caches the "Not Grouped" group id for the whole
    run (issue #513 items 4, 6; round-3 item 3).

    Lives here, not in ldap_sync_service, so create_ldap_user's own
    internal auto-assign and ldap_sync_service's explicit add loop can
    share ONE instance and therefore one resolved value, with NO sentinel
    anywhere: create_ldap_user's not_grouped parameter is simply
    Optional, and None means "no resolver was supplied, look it up the
    plain way" -- a real, distinct code path, not a stand-in for
    "unresolved" masquerading as the type's own value space.

    The FIRST actual need, whichever comes first (an explicit add in the
    reconciler's add loop, or a fresh provisioning call that reaches
    create_ldap_user's own internal auto-assign), resolves it once via a
    single by-name lookup; every later need this run, from EITHER path,
    reuses the cached value. None is a valid, cached answer (the group
    does not exist): both consuming paths treat that as "skip the
    auto-assign", exactly matching the behavior with no "Not Grouped"
    group configured.
    """

    def __init__(self) -> None:
        self._resolved = False
        self._id: uuid.UUID | None = None

    async def get(self, db: AsyncSession) -> uuid.UUID | None:
        if not self._resolved:
            from app.services.group_service import get_group_by_name

            not_grouped = await get_group_by_name(db, "Not Grouped")
            self._id = not_grouped.id if not_grouped else None
            self._resolved = True
        return self._id


async def _auto_assign_not_grouped(
    db: AsyncSession,
    user: User,
    not_grouped: NotGroupedResolver | None = None,
) -> None:
    # Captured before anything below can fail: a failed FLUSH (not just an
    # explicit rollback) already expires every instance the session tracks,
    # so username must be read now, before the try, or not safely at all.
    username = user.username
    try:
        from app.services.group_service import _add_member_resolved, get_group_by_name

        if not_grouped is None:
            not_grouped_group = await get_group_by_name(db, "Not Grouped")
            resolved_id = not_grouped_group.id if not_grouped_group else None
        else:
            resolved_id = await not_grouped.get(db)
        if resolved_id is not None:
            # Adding directly INTO "Not Grouped": pass resolved_id as both
            # the target group and the not_grouped_id so
            # _add_member_resolved's own group_id != not_grouped_id check
            # correctly skips the (meaningless) self-removal, exactly like
            # the original add_member(db, not_grouped.id, user.id) call did.
            await _add_member_resolved(db, resolved_id, user.id, resolved_id)
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
    full_name: str | None = None,
) -> User:
    user = User(
        email=email,
        username=username,
        full_name=full_name or username,
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
    *,
    not_grouped: NotGroupedResolver | None = None,
) -> User:
    """Provision a new LDAP-sourced user.

    not_grouped (issue #513 item 6; round-3 item 3): a caller can supply
    its own NotGroupedResolver instance so the auto-assign below reuses
    that SAME resolved (and cached) "Not Grouped" id instead of doing its
    own independent by-name lookup. The LDAP reconciler passes its
    run-level resolver here so this provisioning path and its own explicit
    add loop agree on one resolved value, computed at most once per run,
    on whichever of them needs it first. Omitted (None, the default): the
    plain by-name lookup runs every call, exactly as before this
    parameter existed.
    """
    user = User(
        email=email,
        username=username,
        # Phase 1 reads no directory display-name attribute (issue #1252), so
        # an LDAP account's name starts as its username and the user may edit
        # it; sync never writes full_name.
        full_name=username,
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
    await _auto_assign_not_grouped(db, user, not_grouped)
    return user


@dataclass(frozen=True)
class FederatedLogin:
    """One verified IdP sign-in, as provisioning sees it (ADR 0016, D4).

    Defined here, not in the OIDC protocol module, so this module never imports
    it; the callback (phase 3) maps a verified ID token onto it: `username` is
    the configured claim's value, or the email when the claim is absent;
    `full_name` is the `name` claim truncated to 200 characters, or the
    username. `email_verified` is None when the token carries no such claim.

    Every field is untrusted IdP content: `resolve_federated_user` validates
    each one before any of it reaches a query, so it returns a User or None for
    every value and never raises on claim content.
    """

    issuer: str
    subject: str
    email: str | None
    email_verified: bool | None
    username: str | None
    full_name: str | None


# Column widths of users.email, users.username, and users.full_name. A longer
# value is refused (email, username) or truncated (full_name) here, never left
# to the database to reject. The issuer column is Text, but it sits in a
# unique index with the subject, so it gets the subject's bound.
_EMAIL_MAX_LENGTH = 255
_USERNAME_MAX_LENGTH = 100
_FULL_NAME_MAX_LENGTH = 200
_ISSUER_MAX_LENGTH = 255

# Unicode categories Postgres cannot store in a text column through asyncpg:
# control characters (Cc, NUL among them, which Postgres rejects) and lone
# surrogates (Cs, which cannot be encoded as UTF-8). SQLite stores both, so
# only the Postgres-live suite can show the raise these checks prevent.
_UNSTORABLE = frozenset({"Cc", "Cs"})
# For an email or username also the invisible format characters (Cf, the
# zero-width space among them), which would let a look-alike of an existing
# account pass the collision check.
_FORBIDDEN_IN_NAMES = _UNSTORABLE | {"Cf"}


def _has_category(value: str, categories: frozenset[str]) -> bool:
    return any(unicodedata.category(ch) in categories for ch in value)


def _clean_full_name(raw: object, username: str) -> str:
    """The display name to store: each Cc or Cs character becomes a space, the
    result is stripped and cut to 200 characters, and an empty result (or a
    non-string) falls back to the username."""
    if not isinstance(raw, str):
        return username
    cleaned = "".join(" " if unicodedata.category(ch) in _UNSTORABLE else ch for ch in raw)
    return cleaned.strip()[:_FULL_NAME_MAX_LENGTH].strip() or username


def _refuse_federated(reason: str, *, user_id: uuid.UUID | None = None) -> None:
    """Log one refused federated login and return None for the caller to pass on.

    Only the fixed reason and, when an account is already known, its HERD id are
    logged: never the email, username, name, issuer, or subject the IdP sent.
    """
    extra: dict[str, str] = {"action": "login_failure", "source": "oidc", "reason": reason}
    if user_id is not None:
        extra["user_id"] = str(user_id)
    logger.warning("OIDC login refused: %s", reason, extra=extra)
    return None


async def create_oidc_user(db: AsyncSession, login: FederatedLogin) -> User:
    """Create an `oidc` account and its federated identity in ONE transaction.

    `auth_source="oidc"`, no password hash, role user, `full_name` from the
    login (control characters replaced, truncated to 200 characters) or the
    username; then the "Not Grouped" auto-assign every new account gets. On an
    IntegrityError (a concurrent first login of the same identity, or another
    account taking the email or username in the window) the transaction is
    rolled back, neither row survives, and the error is raised for the caller.
    It does NOT validate the email, username, issuer, or subject: callers go
    through `resolve_federated_user`, which does.
    """
    if not login.email or not login.username:
        raise ValueError("an oidc account needs an email and a username")
    full_name = _clean_full_name(login.full_name, login.username)
    user = User(
        email=login.email,
        username=login.username,
        full_name=full_name,
        hashed_password=None,
        auth_source="oidc",
        role=Role.USER,
    )
    db.add(user)
    try:
        await db.flush()
        db.add(FederatedIdentity(user_id=user.id, issuer=login.issuer, subject=login.subject))
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise
    await db.refresh(user)
    logger.info(
        "OIDC user provisioned",
        extra={"action": "oidc_provision", "user_id": str(user.id)},
    )
    await _auto_assign_not_grouped(db, user)
    return user


async def _find_federated_identity(
    db: AsyncSession, issuer: str, subject: str
) -> FederatedIdentity | None:
    result = await db.execute(
        select(FederatedIdentity).where(
            FederatedIdentity.issuer == issuer, FederatedIdentity.subject == subject
        )
    )
    return result.scalar_one_or_none()


async def _returning_federated_login(db: AsyncSession, identity: FederatedIdentity) -> User | None:
    """The federated row decides (D4): nothing stored on the user is updated,
    only the identity's last_login_at, and only for a login that is admitted."""
    identity_id = identity.id
    user_id = identity.user_id
    user = await get_user_by_id(db, user_id)
    if user is None:
        reason = "user_missing"
    elif user.auth_source != "oidc":
        # Cannot exist (both rows are created together); refused defensively.
        reason = "wrong_auth_source"
    elif not user.is_active:
        reason = "inactive"
    else:
        reason = None
    if reason is not None:
        await db.rollback()
        return _refuse_federated(reason, user_id=user_id)
    await db.execute(
        update(FederatedIdentity)
        .where(FederatedIdentity.id == identity_id)
        .values(last_login_at=datetime.now(timezone.utc))
    )
    await db.commit()
    await db.refresh(user)
    return user


async def _email_taken(db: AsyncSession, email: str) -> bool:
    # lower() on BOTH sides, in the database: Python's str.lower() and the
    # database's lower() disagree outside ASCII (SQLite folds ASCII only).
    result = await db.execute(
        select(User.id).where(func.lower(User.email) == func.lower(email)).limit(1)
    )
    return result.scalar_one_or_none() is not None


async def _username_taken(db: AsyncSession, username: str) -> bool:
    result = await db.execute(
        select(User.id).where(func.lower(User.username) == func.lower(username)).limit(1)
    )
    return result.scalar_one_or_none() is not None


def _identity_key_refusal(login: FederatedLogin) -> str | None:
    """Checks on (issuer, subject), run BEFORE the identity lookup, since a
    value Postgres cannot store would make the lookup itself raise."""
    for reason, value, limit in (
        ("issuer_invalid", login.issuer, _ISSUER_MAX_LENGTH),
        ("subject_invalid", login.subject, SUBJECT_MAX_LENGTH),
    ):
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > limit
            or _has_category(value, _UNSTORABLE)
        ):
            return reason
    return None


def _name_is_clean(value: str) -> bool:
    return value == value.strip() and not _has_category(value, _FORBIDDEN_IN_NAMES)


def _first_login_refusal(login: FederatedLogin) -> str | None:
    """The D4 checks that need no database, as a fixed reason, or None.

    Email: absent, empty, or whitespace only is `email_missing`; an
    `email_verified` that is present and not true is `email_unverified` (an
    absent claim is accepted, ADR 0016 D4); leading or trailing whitespace, a
    Cc, Cf, or Cs character, or anything but exactly one `@` with text on both
    sides is `email_invalid`; over 255 characters is `email_too_long`. The
    username likewise: `username_missing`, `username_invalid`,
    `username_too_long` (over 100). No other character rule applies yet.
    """
    email = login.email
    if not isinstance(email, str) or not email.strip():
        return "email_missing"
    if login.email_verified is not None and login.email_verified is not True:
        return "email_unverified"
    local, _, domain = email.partition("@")
    if not _name_is_clean(email) or email.count("@") != 1 or not local or not domain:
        return "email_invalid"
    if len(email) > _EMAIL_MAX_LENGTH:
        return "email_too_long"
    username = login.username
    if not isinstance(username, str) or not username.strip():
        return "username_missing"
    if not _name_is_clean(username):
        return "username_invalid"
    if len(username) > _USERNAME_MAX_LENGTH:
        return "username_too_long"
    return None


async def _lock_first_login_names(db: AsyncSession, email: str, username: str) -> None:
    """Serialize first logins that could collide, on Postgres only.

    Takes transaction-scoped advisory locks (`pg_advisory_xact_lock`) keyed on
    the case-folded email and the case-folded username, in ascending key order
    so two logins can never wait on each other in a cycle. They are held until
    the caller's commit (the insert) or rollback (a refusal), so of concurrent
    first logins whose emails or usernames differ only in case, one checks and
    inserts at a time and the others then see its row. A no-op on any other
    dialect (`herd_common.advisory_lock.xact_lock`): the SQLite unit suites
    cannot prove this; the Postgres-live suite does. The fold is Python's
    `str.casefold`, which equates at least every pair SQL `lower()` equates
    for ASCII; outside ASCII the database's `lower()` may differ.
    """
    keys = sorted(
        {
            advisory_key_from_string("oidc-first-login-email:" + email.casefold()),
            advisory_key_from_string("oidc-first-login-username:" + username.casefold()),
        }
    )
    for key in keys:
        await xact_lock(db, key)


async def resolve_federated_user(db: AsyncSession, login: FederatedLogin) -> User | None:
    """The HERD account for a verified IdP login, or None (ADR 0016, D4).

    Returns the User or None for every input and never raises on claim
    content: the issuer and subject are checked before the identity lookup,
    and every other claim before anything is written. A known (issuer,
    subject) returns its user when that user is an active `oidc` account. A
    first login provisions a new account only when the email and username pass
    `_first_login_refusal` and no account of ANY source holds the email
    (compared case-insensitively) or the username (likewise); a collision
    refuses and never links. On Postgres the check and the insert run under
    `_lock_first_login_names`, so the case-insensitive refusal also holds
    between concurrent first logins. Every refusal is logged once with a
    fixed `reason` and returns None. Every refusal ends with a rollback; a
    success commits its writes and then reloads the returned user, which
    leaves a read transaction open, as `create_user` does.

    A concurrent first login of the same identity is not a collision: when a
    collision check or the insert loses to it, the identity is looked up once
    more and, when it now exists, the login proceeds as a returning one. So N
    concurrent first logins create one account and every one of them gets it.
    """
    reason = _identity_key_refusal(login)
    if reason is not None:
        await db.rollback()
        return _refuse_federated(reason)
    identity = await _find_federated_identity(db, login.issuer, login.subject)
    if identity is not None:
        return await _returning_federated_login(db, identity)

    reason = _first_login_refusal(login)
    if reason is not None:
        await db.rollback()
        return _refuse_federated(reason)

    await _lock_first_login_names(db, login.email, login.username)
    collision = None
    if await _email_taken(db, login.email):
        collision = "email_taken"
    elif await _username_taken(db, login.username):
        collision = "username_taken"
    if collision is None:
        try:
            return await create_oidc_user(db, login)
        except IntegrityError:
            collision = "provision_conflict"

    # Lost to a concurrent first login of this same identity? Then it is a
    # returning login now; otherwise the collision is with another account.
    await db.rollback()
    identity = await _find_federated_identity(db, login.issuer, login.subject)
    if identity is not None:
        return await _returning_federated_login(db, identity)
    await db.rollback()
    return _refuse_federated(collision)


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


# The profile columns a profile write may change (issue #1252). The routes'
# request schema accepts only these keys; this set is the second line, so a
# caller handing update_user_profile any other column name gets a ValueError
# instead of a silent write to role, email, or is_active.
PROFILE_FIELDS = frozenset({"full_name", "contact_email", "phone"})


async def update_user_profile(
    db: AsyncSession,
    target_user_id: uuid.UUID,
    changes: dict[str, str | None],
    *,
    modified_by: uuid.UUID,
) -> tuple[User, list[str]] | None:
    """Apply a profile PATCH to one account (issue #1252).

    `changes` holds only the keys the caller sent (an omitted key is
    unchanged). Every accepted write sets modified_by to the caller, a self
    edit included. Returns the refreshed user and the sorted NAMES of the
    fields whose stored value actually changed, or None for an unknown id.
    """
    unknown = set(changes) - PROFILE_FIELDS
    if unknown:
        raise ValueError(f"Not profile fields: {sorted(unknown)}")
    user = await get_user_by_id(db, target_user_id)
    if not user:
        return None
    changed = sorted(name for name, value in changes.items() if getattr(user, name) != value)
    for name, value in changes.items():
        setattr(user, name, value)
    user.modified_by = modified_by
    await db.commit()
    await db.refresh(user)
    # Field NAMES only, never values: contact_email and phone are personal
    # data, and key-name redaction cannot see inside a value.
    logger.info(
        "User profile updated: %s by %s",
        target_user_id,
        modified_by,
        extra={
            "action": "user_profile_updated",
            "user_id": str(target_user_id),
            "changed_fields": changed,
        },
    )
    return user, changed


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
