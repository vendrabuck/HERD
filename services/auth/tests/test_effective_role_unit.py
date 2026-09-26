"""Unit tests for app.dependencies.auth.effective_role and its dependency wiring.

effective_role is the pure function that decides, for every authorization
decision inside the auth service, which role a request is held to: the
lower of the JWT's role claim and the account's current database role.
Enumerated over the full space rather than a handful of examples, because
the invariant this function restores (a demoted or downscoped token can
never regain the account's higher database role inside this service) is
exactly the kind of thing a partial test suite would miss a corner of.

The second half of this file covers _decode_role_claim and get_effective_role
directly: the split between "no Authorization header at all" (pass the
database role through; only a test double reaches this) and "a header is
present but is not a decodable bearer token" (fail closed to user) is the
hardening that keeps this mechanism fail-closed even if get_current_user is
ever changed to accept credentials some other way.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from app.config import settings
from app.dependencies.auth import (
    _ClaimAvailability,
    _decode_role_claim,
    effective_role,
    get_effective_role,
)
from app.models.user import Role, User
from app.utils.jwt import create_access_token
from jose import jwt as jose_jwt
from starlette.requests import Request

ROLES = [Role.USER, Role.ADMIN, Role.SUPERADMIN]


def _request(authorization: str | None) -> Request:
    headers = [(b"authorization", authorization.encode())] if authorization is not None else []
    return Request({"type": "http", "headers": headers})


def _fake_user(role: Role) -> User:
    return User(
        id=uuid.uuid4(),
        email="fake@test.com",
        username="fake",
        hashed_password="fake",
        is_active=True,
        role=role,
    )


def _expired_token(role: str = "superadmin") -> str:
    payload = {
        "sub": str(uuid.uuid4()),
        "role": role,
        "exp": datetime.now(timezone.utc) - timedelta(minutes=5),
    }
    return jose_jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


@pytest.mark.parametrize("db_role", ROLES)
@pytest.mark.parametrize("claim_role", ROLES)
def test_effective_role_is_the_lower_of_claim_and_db_role(claim_role, db_role):
    """All nine (claim, db) pairs: the exact returned role, not just "not higher"."""
    rank = {Role.USER: 0, Role.ADMIN: 1, Role.SUPERADMIN: 2}
    expected = claim_role if rank[claim_role] <= rank[db_role] else db_role
    assert effective_role(claim_role.value, db_role) == expected


@pytest.mark.parametrize("db_role", ROLES)
def test_missing_claim_counts_as_user(db_role):
    assert effective_role(None, db_role) == Role.USER


@pytest.mark.parametrize("db_role", ROLES)
def test_empty_claim_counts_as_user(db_role):
    assert effective_role("", db_role) == Role.USER


@pytest.mark.parametrize("db_role", ROLES)
def test_unknown_claim_counts_as_user(db_role):
    assert effective_role("not-a-real-role", db_role) == Role.USER


def test_matching_claim_and_db_role_is_a_no_op():
    """No behavior change for a request whose claim equals the DB role."""
    for role in ROLES:
        assert effective_role(role.value, role) == role


def test_claim_never_exceeds_db_role():
    """A user-claim token on a superadmin row stays at user; the reported defect."""
    assert effective_role(Role.USER.value, Role.SUPERADMIN) == Role.USER


def test_db_role_never_exceeds_claim():
    """A superadmin-claim token on a since-demoted user row is held to user."""
    assert effective_role(Role.SUPERADMIN.value, Role.USER) == Role.USER


# --- _decode_role_claim: header parsing ---


@pytest.mark.asyncio
async def test_decode_role_claim_no_header_at_all():
    assert await _decode_role_claim(_request(None)) is _ClaimAvailability.NO_HEADER


@pytest.mark.asyncio
async def test_decode_role_claim_wrong_scheme_is_undecodable():
    assert await _decode_role_claim(_request("Basic x")) is _ClaimAvailability.UNDECODABLE


@pytest.mark.asyncio
async def test_decode_role_claim_bearer_with_empty_token_is_undecodable():
    assert await _decode_role_claim(_request("Bearer")) is _ClaimAvailability.UNDECODABLE
    assert await _decode_role_claim(_request("Bearer ")) is _ClaimAvailability.UNDECODABLE


@pytest.mark.asyncio
async def test_decode_role_claim_garbage_token_is_undecodable():
    assert (
        await _decode_role_claim(_request("Bearer not-a-real-token"))
        is _ClaimAvailability.UNDECODABLE
    )


@pytest.mark.asyncio
async def test_decode_role_claim_expired_token_is_undecodable():
    token = _expired_token()
    assert await _decode_role_claim(_request(f"Bearer {token}")) is _ClaimAvailability.UNDECODABLE


@pytest.mark.asyncio
async def test_decode_role_claim_valid_token_returns_the_claim():
    token = create_access_token({"sub": str(uuid.uuid4()), "role": "admin"})
    assert await _decode_role_claim(_request(f"Bearer {token}")) == "admin"


@pytest.mark.asyncio
async def test_decode_role_claim_lowercase_scheme_matches_titlecase():
    """The scheme comparison is case-insensitive, matching HTTPBearer's own rule."""
    token = create_access_token({"sub": str(uuid.uuid4()), "role": "admin"})
    assert await _decode_role_claim(_request(f"bearer {token}")) == "admin"
    assert await _decode_role_claim(_request(f"Bearer {token}")) == "admin"


# --- get_effective_role: the three branches, with a fake (unsaved) User ---


@pytest.mark.asyncio
async def test_get_effective_role_no_header_passes_db_role_through():
    """Only reachable when get_current_user has been replaced by a test
    double; the pass-through this file's HTTP-level tests exercise for real."""
    user = _fake_user(Role.SUPERADMIN)
    role = await get_effective_role(current_user=user, role_claim=_ClaimAvailability.NO_HEADER)
    assert role == Role.SUPERADMIN


@pytest.mark.asyncio
async def test_get_effective_role_undecodable_fails_closed_to_user_even_for_superadmin():
    """The hardening under test: a header present but undecodable must NOT
    fall back to the database role, even for a superadmin row."""
    user = _fake_user(Role.SUPERADMIN)
    role = await get_effective_role(current_user=user, role_claim=_ClaimAvailability.UNDECODABLE)
    assert role == Role.USER


@pytest.mark.asyncio
async def test_get_effective_role_decoded_claim_uses_effective_role():
    user = _fake_user(Role.SUPERADMIN)
    role = await get_effective_role(current_user=user, role_claim="user")
    assert role == Role.USER

    role = await get_effective_role(current_user=user, role_claim="superadmin")
    assert role == Role.SUPERADMIN
