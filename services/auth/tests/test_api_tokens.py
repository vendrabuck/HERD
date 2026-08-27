import uuid
from datetime import datetime, timedelta, timezone

import pytest
from app.models.api_token import ApiToken
from app.models.user import Role, User
from app.services.api_token_service import (
    create_api_token,
    exchange_api_token,
    revoke_api_token,
)
from app.utils.jwt import hash_token, verify_access_token
from sqlalchemy import select

from tests._harness import TestSessionLocal, mock_user


async def _make_user(
    role: Role = Role.USER, *, is_active: bool = True, username: str = "svc"
) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"{username}@test.com",
        username=username,
        hashed_password="fake",
        is_active=is_active,
        role=role,
    )
    async with TestSessionLocal() as session:
        session.add(user)
        await session.commit()
        await session.refresh(user)
    return user


# --- Service-level tests ---


@pytest.mark.asyncio
async def test_create_stores_only_hash_returns_raw_once():
    principal = await _make_user(Role.ADMIN, username="creator")
    async with TestSessionLocal() as session:
        token, raw = await create_api_token(
            session,
            name="ci-bot",
            principal=principal,
            role=Role.USER,
            expires_at=None,
            created_by=principal.id,
        )
        token_id = token.id

    # The persisted row holds only the sha256 hash, never the raw value.
    async with TestSessionLocal() as session:
        stored = (
            await session.execute(select(ApiToken).where(ApiToken.id == token_id))
        ).scalar_one()
        assert stored.token_hash == hash_token(raw)
        assert stored.token_hash != raw
        assert len(stored.token_hash) == 64


@pytest.mark.asyncio
async def test_role_cannot_exceed_principal_role():
    principal = await _make_user(Role.USER, username="lowpriv")
    async with TestSessionLocal() as session:
        with pytest.raises(ValueError):
            await create_api_token(
                session,
                name="escalate",
                principal=principal,
                role=Role.ADMIN,
                expires_at=None,
                created_by=None,
            )


@pytest.mark.asyncio
async def test_role_equal_to_principal_role_allowed():
    principal = await _make_user(Role.ADMIN, username="equalpriv")
    async with TestSessionLocal() as session:
        token, _ = await create_api_token(
            session,
            name="admin-bot",
            principal=principal,
            role=Role.ADMIN,
            expires_at=None,
            created_by=None,
        )
        assert token.role == Role.ADMIN


@pytest.mark.asyncio
async def test_exchange_valid_token_mints_jwt():
    principal = await _make_user(Role.ADMIN, username="exch")
    async with TestSessionLocal() as session:
        _, raw = await create_api_token(
            session,
            name="exchanger",
            principal=principal,
            role=Role.USER,
            expires_at=None,
            created_by=None,
        )

    async with TestSessionLocal() as session:
        jwt_str = await exchange_api_token(session, raw)

    assert jwt_str is not None
    payload = verify_access_token(jwt_str)
    assert payload["sub"] == str(principal.id)
    assert payload["role"] == Role.USER.value
    assert payload["auth_source"] == "api_token"
    assert payload["username"] == principal.username
    assert payload["email"] == principal.email
    # Machines re-exchange; no refresh token concept is encoded.
    assert "exp" in payload


@pytest.mark.asyncio
async def test_exchange_clamps_role_when_principal_demoted():
    # An admin-role token for an admin principal, then the principal is demoted.
    principal = await _make_user(Role.ADMIN, username="demoted")
    async with TestSessionLocal() as session:
        _, raw = await create_api_token(
            session,
            name="admin-bot",
            principal=principal,
            role=Role.ADMIN,
            expires_at=None,
            created_by=None,
        )

    # Demote the owning principal to a plain user after the token was minted.
    async with TestSessionLocal() as session:
        from sqlalchemy import update

        await session.execute(update(User).where(User.id == principal.id).values(role=Role.USER))
        await session.commit()

    async with TestSessionLocal() as session:
        jwt_str = await exchange_api_token(session, raw)

    assert jwt_str is not None
    payload = verify_access_token(jwt_str)
    # The minted JWT carries the clamped (current) role, not the snapshotted admin role.
    assert payload["role"] == Role.USER.value


@pytest.mark.asyncio
async def test_exchange_role_unchanged_when_principal_role_unchanged():
    # The normal case: the principal's role is unchanged, so the token role stands.
    principal = await _make_user(Role.ADMIN, username="steady")
    async with TestSessionLocal() as session:
        _, raw = await create_api_token(
            session,
            name="admin-bot",
            principal=principal,
            role=Role.ADMIN,
            expires_at=None,
            created_by=None,
        )

    async with TestSessionLocal() as session:
        jwt_str = await exchange_api_token(session, raw)

    assert jwt_str is not None
    payload = verify_access_token(jwt_str)
    assert payload["role"] == Role.ADMIN.value


@pytest.mark.asyncio
async def test_exchange_keeps_lower_token_role_below_principal():
    # The clamp is min, not "principal's role": a token whose role is lower than
    # the principal's current role still mints the token's lower role.
    principal = await _make_user(Role.ADMIN, username="lowertoken")
    async with TestSessionLocal() as session:
        _, raw = await create_api_token(
            session,
            name="user-bot",
            principal=principal,
            role=Role.USER,
            expires_at=None,
            created_by=None,
        )

    async with TestSessionLocal() as session:
        jwt_str = await exchange_api_token(session, raw)

    assert jwt_str is not None
    payload = verify_access_token(jwt_str)
    assert payload["role"] == Role.USER.value


@pytest.mark.asyncio
async def test_exchange_sets_last_used_at():
    principal = await _make_user(Role.USER, username="lastused")
    async with TestSessionLocal() as session:
        token, raw = await create_api_token(
            session,
            name="bot",
            principal=principal,
            role=Role.USER,
            expires_at=None,
            created_by=None,
        )
        token_id = token.id
        assert token.last_used_at is None

    async with TestSessionLocal() as session:
        assert await exchange_api_token(session, raw) is not None

    async with TestSessionLocal() as session:
        refreshed = (
            await session.execute(select(ApiToken).where(ApiToken.id == token_id))
        ).scalar_one()
        assert refreshed.last_used_at is not None


@pytest.mark.asyncio
async def test_exchange_unknown_token_returns_none():
    async with TestSessionLocal() as session:
        assert await exchange_api_token(session, "no-such-token") is None


@pytest.mark.asyncio
async def test_exchange_revoked_token_returns_none():
    principal = await _make_user(Role.USER, username="revoked")
    async with TestSessionLocal() as session:
        token, raw = await create_api_token(
            session,
            name="bot",
            principal=principal,
            role=Role.USER,
            expires_at=None,
            created_by=None,
        )
        await revoke_api_token(session, token.id)

    async with TestSessionLocal() as session:
        assert await exchange_api_token(session, raw) is None


@pytest.mark.asyncio
async def test_exchange_expired_token_returns_none():
    principal = await _make_user(Role.USER, username="expired")
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    async with TestSessionLocal() as session:
        _, raw = await create_api_token(
            session,
            name="bot",
            principal=principal,
            role=Role.USER,
            expires_at=past,
            created_by=None,
        )

    async with TestSessionLocal() as session:
        assert await exchange_api_token(session, raw) is None


@pytest.mark.asyncio
async def test_exchange_inactive_principal_returns_none():
    principal = await _make_user(Role.USER, username="inactive")
    async with TestSessionLocal() as session:
        _, raw = await create_api_token(
            session,
            name="bot",
            principal=principal,
            role=Role.USER,
            expires_at=None,
            created_by=None,
        )

    # Deactivate the owning principal.
    async with TestSessionLocal() as session:
        from sqlalchemy import update

        await session.execute(update(User).where(User.id == principal.id).values(is_active=False))
        await session.commit()

    async with TestSessionLocal() as session:
        assert await exchange_api_token(session, raw) is None


@pytest.mark.asyncio
async def test_revoke_is_idempotent():
    principal = await _make_user(Role.USER, username="idem")
    async with TestSessionLocal() as session:
        token, _ = await create_api_token(
            session,
            name="bot",
            principal=principal,
            role=Role.USER,
            expires_at=None,
            created_by=None,
        )
        token_id = token.id

    async with TestSessionLocal() as session:
        await revoke_api_token(session, token_id)
        await revoke_api_token(session, token_id)
        # Revoking a nonexistent token is also a no-op.
        await revoke_api_token(session, uuid.uuid4())

    async with TestSessionLocal() as session:
        stored = (
            await session.execute(select(ApiToken).where(ApiToken.id == token_id))
        ).scalar_one()
        assert stored.is_active is False


# --- HTTP / RBAC tests ---


@pytest.fixture
async def admin_client(make_client):
    async with make_client(mock_user(Role.ADMIN, username="adminuser")) as ac:
        yield ac


@pytest.fixture
async def regular_client(make_client):
    async with make_client(mock_user(Role.USER, username="regular")) as ac:
        yield ac


@pytest.fixture
async def superadmin_client(make_client):
    async with make_client(mock_user(Role.SUPERADMIN, username="superuser")) as ac:
        yield ac


@pytest.fixture
async def anon_client(make_client):
    async with make_client() as ac:
        yield ac


@pytest.mark.asyncio
async def test_admin_create_returns_raw_token_once(admin_client):
    principal = await _make_user(Role.ADMIN, username="httpprincipal")
    resp = await admin_client.post(
        "/tokens",
        json={"name": "ci", "principal_id": str(principal.id), "role": "user"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["token"]
    assert data["role"] == "user"
    assert data["principal_id"] == str(principal.id)

    # The raw token exchanges successfully.
    raw = data["token"]
    exch = await admin_client.post("/tokens/exchange", json={"token": raw})
    assert exch.status_code == 200
    assert exch.json()["token_type"] == "bearer"
    assert exch.json()["expires_in"] > 0

    # The list endpoint never exposes the raw value or the hash.
    listed = await admin_client.get("/tokens")
    assert listed.status_code == 200
    items = listed.json()
    assert len(items) == 1
    assert "token" not in items[0]
    assert "token_hash" not in items[0]


@pytest.mark.asyncio
async def test_create_unknown_principal_404(admin_client):
    resp = await admin_client.post(
        "/tokens",
        json={"name": "ci", "principal_id": str(uuid.uuid4()), "role": "user"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_create_role_exceeds_principal_400(admin_client):
    principal = await _make_user(Role.USER, username="lowprivhttp")
    resp = await admin_client.post(
        "/tokens",
        json={"name": "esc", "principal_id": str(principal.id), "role": "admin"},
    )
    assert resp.status_code == 400


# --- Caller-role escalation guard (issue #312) ---


@pytest.mark.asyncio
async def test_admin_cannot_mint_superadmin_token(admin_client):
    """The #312 escalation: an admin selects the superadmin as principal with a
    superadmin role. The service's role-vs-principal check passes (superadmin
    does not exceed superadmin), so only the caller-role guard stops it. Must be
    403, and no token may be persisted (a created row could still be exchanged)."""
    superadmin = await _make_user(Role.SUPERADMIN, username="thesuper")
    resp = await admin_client.post(
        "/tokens",
        json={"name": "pwn", "principal_id": str(superadmin.id), "role": "superadmin"},
    )
    assert resp.status_code == 403

    # Nothing was created: the escalation is fully blocked, not merely un-returned.
    async with TestSessionLocal() as session:
        rows = (await session.execute(select(ApiToken))).scalars().all()
        assert rows == []


@pytest.mark.asyncio
async def test_admin_cannot_mint_for_superadmin_principal_even_at_admin_role(admin_client):
    """Impersonation via a higher-ranked principal: even requesting only an
    admin role, choosing the superadmin as principal is refused, because the
    exchange clamps to min(token_role, principal_role) and the principal axis is
    what an admin must not reach above."""
    superadmin = await _make_user(Role.SUPERADMIN, username="super2")
    resp = await admin_client.post(
        "/tokens",
        json={"name": "x", "principal_id": str(superadmin.id), "role": "admin"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_admin_cannot_request_superadmin_role(admin_client):
    """Requesting a role above the caller is refused with 403 (the caller-role
    guard), which fires before the 400 role-vs-principal check."""
    principal = await _make_user(Role.ADMIN, username="adminprin")
    resp = await admin_client.post(
        "/tokens",
        json={"name": "x", "principal_id": str(principal.id), "role": "superadmin"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_superadmin_can_mint_superadmin_token(superadmin_client):
    """Regression: the superadmin retains the ability the guard must not remove."""
    superadmin = await _make_user(Role.SUPERADMIN, username="super3")
    resp = await superadmin_client.post(
        "/tokens",
        json={"name": "ok", "principal_id": str(superadmin.id), "role": "superadmin"},
    )
    assert resp.status_code == 201
    assert resp.json()["role"] == "superadmin"


@pytest.mark.asyncio
async def test_admin_can_mint_admin_token_for_admin_principal(admin_client):
    """Equal-rank is allowed (documented policy, issue #312): an admin may mint
    an admin-role token for another admin machine principal. Only strictly
    higher rank is refused."""
    other_admin = await _make_user(Role.ADMIN, username="otheradmin")
    resp = await admin_client.post(
        "/tokens",
        json={"name": "peer", "principal_id": str(other_admin.id), "role": "admin"},
    )
    assert resp.status_code == 201
    assert resp.json()["role"] == "admin"


@pytest.mark.asyncio
async def test_non_admin_cannot_create(regular_client):
    principal = await _make_user(Role.USER, username="p1")
    resp = await regular_client.post(
        "/tokens",
        json={"name": "ci", "principal_id": str(principal.id), "role": "user"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_non_admin_cannot_list(regular_client):
    resp = await regular_client.get("/tokens")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_non_admin_cannot_delete(regular_client):
    resp = await regular_client.delete(f"/tokens/{uuid.uuid4()}")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_admin_delete_revokes(admin_client):
    principal = await _make_user(Role.USER, username="delprincipal")
    create = await admin_client.post(
        "/tokens",
        json={"name": "ci", "principal_id": str(principal.id), "role": "user"},
    )
    token_id = create.json()["id"]
    raw = create.json()["token"]

    resp = await admin_client.delete(f"/tokens/{token_id}")
    assert resp.status_code == 204

    # A revoked token can no longer be exchanged.
    exch = await admin_client.post("/tokens/exchange", json={"token": raw})
    assert exch.status_code == 401


@pytest.mark.asyncio
async def test_exchange_needs_no_auth(anon_client):
    principal = await _make_user(Role.USER, username="anonprincipal")
    async with TestSessionLocal() as session:
        _, raw = await create_api_token(
            session,
            name="bot",
            principal=principal,
            role=Role.USER,
            expires_at=None,
            created_by=None,
        )
    resp = await anon_client.post("/tokens/exchange", json={"token": raw})
    assert resp.status_code == 200
    assert "access_token" in resp.json()


@pytest.mark.asyncio
async def test_exchange_bad_token_generic_401(anon_client):
    resp = await anon_client.post("/tokens/exchange", json={"token": "garbage"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid or expired token"
