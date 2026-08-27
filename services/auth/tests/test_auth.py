import uuid
from datetime import datetime, timedelta, timezone

import pytest
from app.models.user import RefreshToken, Role, User
from app.utils.jwt import create_access_token, hash_token

from tests._harness import TestSessionLocal, mock_user


@pytest.fixture
async def client(make_client):
    async with make_client() as ac:
        yield ac


@pytest.mark.asyncio
async def test_register(client):
    resp = await client.post(
        "/register",
        json={"email": "test@example.com", "username": "testuser", "password": "secret123"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["email"] == "test@example.com"
    assert data["username"] == "testuser"


@pytest.mark.asyncio
async def test_register_duplicate_email(client):
    payload = {"email": "dup@example.com", "username": "user1", "password": "password123"}
    await client.post("/register", json=payload)
    resp = await client.post(
        "/register",
        json={"email": "dup@example.com", "username": "user2", "password": "password123"},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_login(client):
    await client.post(
        "/register",
        json={"email": "login@example.com", "username": "loginuser", "password": "mypassword"},
    )
    resp = await client.post(
        "/login", json={"email": "login@example.com", "password": "mypassword"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data
    assert "refresh_token" in data


@pytest.mark.asyncio
async def test_login_wrong_password(client):
    await client.post(
        "/register",
        json={"email": "wrong@example.com", "username": "wronguser", "password": "correct123"},
    )
    resp = await client.post(
        "/login", json={"email": "wrong@example.com", "password": "incorrect1"}
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_me(client):
    await client.post(
        "/register",
        json={"email": "me@example.com", "username": "meuser", "password": "pass12345"},
    )
    login_resp = await client.post(
        "/login", json={"email": "me@example.com", "password": "pass12345"}
    )
    token = login_resp.json()["access_token"]
    resp = await client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["email"] == "me@example.com"


@pytest.mark.asyncio
async def test_refresh(client):
    await client.post(
        "/register",
        json={"email": "refresh@example.com", "username": "refreshuser", "password": "password123"},
    )
    login_resp = await client.post(
        "/login", json={"email": "refresh@example.com", "password": "password123"}
    )
    refresh_token = login_resp.json()["refresh_token"]
    resp = await client.post("/refresh", json={"refresh_token": refresh_token})
    assert resp.status_code == 200
    assert "access_token" in resp.json()


# --- Admin endpoint tests ---

_superadmin_id = uuid.uuid4()
_admin_id = uuid.uuid4()


@pytest.fixture
async def superadmin_client(make_client):
    """Client authenticated as superadmin for admin endpoint tests."""
    async with make_client(
        mock_user(Role.SUPERADMIN, user_id=_superadmin_id, username="superadmin")
    ) as ac:
        yield ac


@pytest.fixture
async def regular_client(make_client):
    """Client authenticated as regular user for admin endpoint tests."""
    async with make_client(mock_user(Role.USER, username="regular")) as ac:
        yield ac


@pytest.mark.asyncio
async def test_superadmin_can_list_users(superadmin_client):
    resp = await superadmin_client.get("/users")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data["items"], list)
    assert "total" in data
    assert "skip" in data
    assert "limit" in data


@pytest.mark.asyncio
async def test_superadmin_can_change_role(superadmin_client):
    # Register a user first, then promote to admin
    await superadmin_client.post(
        "/register",
        json={"email": "target@test.com", "username": "targetuser", "password": "password123"},
    )
    users_resp = await superadmin_client.get("/users")
    target = [u for u in users_resp.json()["items"] if u["email"] == "target@test.com"][0]

    resp = await superadmin_client.put(
        f"/users/{target['id']}/role",
        json={"role": "admin"},
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "admin"


@pytest.mark.asyncio
async def test_cannot_set_superadmin_role(superadmin_client):
    await superadmin_client.post(
        "/register",
        json={"email": "nosup@test.com", "username": "nosupuser", "password": "password123"},
    )
    users_resp = await superadmin_client.get("/users")
    target = [u for u in users_resp.json()["items"] if u["email"] == "nosup@test.com"][0]

    resp = await superadmin_client.put(
        f"/users/{target['id']}/role",
        json={"role": "superadmin"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_cannot_change_own_role(superadmin_client):
    resp = await superadmin_client.put(
        f"/users/{_superadmin_id}/role",
        json={"role": "user"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_non_superadmin_gets_403(regular_client):
    resp = await regular_client.get("/users")
    assert resp.status_code == 403
    # Pin the exact require_role 403 detail wording.
    assert resp.json()["detail"] == "You do not have permission to perform this action"


# --- Logout and refresh revocation tests ---


@pytest.mark.asyncio
async def test_logout_revokes_refresh_token(client):
    await client.post(
        "/register",
        json={"email": "logout@example.com", "username": "logoutuser", "password": "password123"},
    )
    login_resp = await client.post(
        "/login", json={"email": "logout@example.com", "password": "password123"}
    )
    refresh_token = login_resp.json()["refresh_token"]
    logout_resp = await client.post("/logout", json={"refresh_token": refresh_token})
    assert logout_resp.status_code == 204
    # Subsequent refresh should fail
    refresh_resp = await client.post("/refresh", json={"refresh_token": refresh_token})
    assert refresh_resp.status_code == 401


@pytest.mark.asyncio
async def test_refresh_with_revoked_token(client):
    """After rotation, using the original refresh token should fail."""
    await client.post(
        "/register",
        json={"email": "rotate@example.com", "username": "rotateuser", "password": "password123"},
    )
    login_resp = await client.post(
        "/login", json={"email": "rotate@example.com", "password": "password123"}
    )
    original_token = login_resp.json()["refresh_token"]
    # Rotate: original token is revoked, new token issued
    rotate_resp = await client.post("/refresh", json={"refresh_token": original_token})
    assert rotate_resp.status_code == 200
    # Original token should now be invalid
    retry_resp = await client.post("/refresh", json={"refresh_token": original_token})
    assert retry_resp.status_code == 401
    # Pin the exact 401 detail wording for refresh failures.
    assert retry_resp.json()["detail"] == "Invalid or expired refresh token"


# --- Registration validation tests ---


@pytest.mark.asyncio
async def test_register_duplicate_username(client):
    payload = {"email": "first@example.com", "username": "sameuser", "password": "password123"}
    await client.post("/register", json=payload)
    resp = await client.post(
        "/register",
        json={"email": "second@example.com", "username": "sameuser", "password": "password123"},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_register_password_too_short(client):
    resp = await client.post(
        "/register",
        json={"email": "short@example.com", "username": "shortpw", "password": "1234567"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_register_password_too_long(client):
    resp = await client.post(
        "/register",
        json={"email": "long@example.com", "username": "longpw", "password": "x" * 73},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_register_username_too_short(client):
    resp = await client.post(
        "/register",
        json={"email": "shortu@example.com", "username": "ab", "password": "password123"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_register_username_invalid_chars(client):
    resp = await client.post(
        "/register",
        json={"email": "invalid@example.com", "username": "bad user!", "password": "password123"},
    )
    assert resp.status_code == 422


# --- Auth edge cases ---


@pytest.mark.asyncio
async def test_login_nonexistent_email(client):
    """Login with unregistered email returns 401."""
    resp = await client.post(
        "/login", json={"email": "nobody@example.com", "password": "password123"}
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_logout_with_invalid_token(client):
    """Logout with random refresh_token returns 204 (idempotent)."""
    resp = await client.post("/logout", json={"refresh_token": "totally-bogus-token"})
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_me_without_token(client):
    """GET /me with no Authorization header returns 401."""
    resp = await client.get("/me")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_me_with_invalid_token(client):
    """GET /me with Bearer garbage returns 401."""
    resp = await client.get("/me", headers={"Authorization": "Bearer garbage"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_me_with_non_uuid_subject_returns_401(client):
    """A validly-signed token whose sub is not a UUID must return 401, not 500.

    uuid.UUID() on a non-UUID sub raises ValueError (not a JWTError); the
    dependency must map that to the credentials 401 rather than letting it
    surface as an unhandled 500.
    """
    token = create_access_token({"sub": "not-a-uuid"})
    resp = await client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_register_invalid_email(client):
    """Register with invalid email format returns 422."""
    resp = await client.post(
        "/register",
        json={"email": "notanemail", "username": "validuser", "password": "password123"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_full_auth_lifecycle(client):
    """Register, login, GET /me, refresh, logout, verify refresh token revoked."""
    # Register
    reg = await client.post(
        "/register",
        json={"email": "lifecycle@example.com", "username": "lifecycle", "password": "password123"},
    )
    assert reg.status_code == 201
    # Login
    login = await client.post(
        "/login", json={"email": "lifecycle@example.com", "password": "password123"}
    )
    assert login.status_code == 200
    access = login.json()["access_token"]
    refresh = login.json()["refresh_token"]
    # GET /me
    me = await client.get("/me", headers={"Authorization": f"Bearer {access}"})
    assert me.status_code == 200
    assert me.json()["email"] == "lifecycle@example.com"
    # Refresh
    ref = await client.post("/refresh", json={"refresh_token": refresh})
    assert ref.status_code == 200
    new_refresh = ref.json()["refresh_token"]
    # Logout
    logout = await client.post("/logout", json={"refresh_token": new_refresh})
    assert logout.status_code == 204
    # Verify refresh token revoked
    retry = await client.post("/refresh", json={"refresh_token": new_refresh})
    assert retry.status_code == 401


# --- Additional auth edge case tests ---


@pytest.fixture
async def admin_client(make_client):
    """Client authenticated as admin (not superadmin) for admin endpoint tests."""
    async with make_client(mock_user(Role.ADMIN, user_id=_admin_id, username="adminuser")) as ac:
        yield ac


@pytest.mark.asyncio
async def test_health_endpoint(client):
    """GET /health returns 200."""
    resp = await client.get("/health")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_login_empty_password(client):
    """password="" returns 401 or 422."""
    await client.post(
        "/register",
        json={"email": "emptypass@example.com", "username": "emptypass", "password": "password123"},
    )
    resp = await client.post("/login", json={"email": "emptypass@example.com", "password": ""})
    assert resp.status_code in (401, 422)


@pytest.mark.asyncio
async def test_me_with_empty_bearer(client):
    """'Bearer ' (empty token) returns 401."""
    resp = await client.get("/me", headers={"Authorization": "Bearer "})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_me_without_bearer_prefix(client):
    """'Token abc' (wrong scheme) returns 401."""
    resp = await client.get("/me", headers={"Authorization": "Token abc"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_set_role_to_same_role(superadmin_client):
    """No-op: user already user, set to user again."""
    await superadmin_client.post(
        "/register",
        json={"email": "samerole@test.com", "username": "samerole", "password": "password123"},
    )
    users_resp = await superadmin_client.get("/users")
    target = [u for u in users_resp.json()["items"] if u["email"] == "samerole@test.com"][0]
    assert target["role"] == "user"
    resp = await superadmin_client.put(
        f"/users/{target['id']}/role",
        json={"role": "user"},
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "user"


@pytest.mark.asyncio
async def test_admin_can_list_users(admin_client):
    """Admin can list users (widened from superadmin-only)."""
    resp = await admin_client.get("/users")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_admin_cannot_change_role(admin_client):
    """Admin gets 403 on PUT /users/{id}/role."""
    resp = await admin_client.put(
        f"/users/{uuid.uuid4()}/role",
        json={"role": "admin"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_register_missing_email_field(client):
    """Missing email returns 422."""
    resp = await client.post(
        "/register",
        json={"username": "noemail", "password": "password123"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_register_missing_password_field(client):
    """Missing password returns 422."""
    resp = await client.post(
        "/register",
        json={"email": "nopass@example.com", "username": "nopass"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_refresh_returns_new_access_token(client):
    """New access token is returned; new refresh token differs from original."""
    await client.post(
        "/register",
        json={"email": "newtoken@example.com", "username": "newtoken", "password": "password123"},
    )
    login_resp = await client.post(
        "/login", json={"email": "newtoken@example.com", "password": "password123"}
    )
    refresh_token = login_resp.json()["refresh_token"]
    ref_resp = await client.post("/refresh", json={"refresh_token": refresh_token})
    assert ref_resp.status_code == 200
    assert "access_token" in ref_resp.json()
    # Refresh token is rotated, so the new one differs from the original
    new_refresh = ref_resp.json()["refresh_token"]
    assert new_refresh != refresh_token


@pytest.mark.asyncio
async def test_change_role_invalid_uuid(superadmin_client):
    """PUT /users/not-a-uuid/role returns 422."""
    resp = await superadmin_client.put(
        "/users/not-a-uuid/role",
        json={"role": "admin"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_register_username_max_length(client):
    """33-char username returns 422 (max_length=32)."""
    resp = await client.post(
        "/register",
        json={"email": "long@example.com", "username": "a" * 33, "password": "password123"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_change_role_nonexistent_user(superadmin_client):
    """PUT /users/{random-uuid}/role returns 404 for nonexistent user."""
    resp = await superadmin_client.put(
        f"/users/{uuid.uuid4()}/role",
        json={"role": "admin"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_cannot_change_role_of_another_superadmin(superadmin_client):
    """Cannot change the role of a user who is already superadmin."""
    # Register a user and manually set them as superadmin in DB
    await superadmin_client.post(
        "/register",
        json={"email": "sa2@test.com", "username": "superadmin2", "password": "password123"},
    )
    users_resp = await superadmin_client.get("/users")
    target = [u for u in users_resp.json()["items"] if u["email"] == "sa2@test.com"][0]
    # Set superadmin role directly in DB
    async with TestSessionLocal() as session:
        from sqlalchemy import update

        await session.execute(
            update(User).where(User.id == uuid.UUID(target["id"])).values(role=Role.SUPERADMIN)
        )
        await session.commit()
    # Attempt to change their role should fail
    resp = await superadmin_client.put(
        f"/users/{target['id']}/role",
        json={"role": "user"},
    )
    assert resp.status_code == 400
    assert "superadmin" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_inactive_user_blocked_from_me_and_refresh(client):
    """An inactive user cannot access /me or /refresh."""
    await client.post(
        "/register",
        json={"email": "inactive@example.com", "username": "inactive", "password": "password123"},
    )
    login_resp = await client.post(
        "/login", json={"email": "inactive@example.com", "password": "password123"}
    )
    access_token = login_resp.json()["access_token"]
    refresh_token = login_resp.json()["refresh_token"]
    # Deactivate user in DB
    async with TestSessionLocal() as session:
        from sqlalchemy import update

        await session.execute(
            update(User).where(User.email == "inactive@example.com").values(is_active=False)
        )
        await session.commit()
    # /me should return 401
    me_resp = await client.get("/me", headers={"Authorization": f"Bearer {access_token}"})
    assert me_resp.status_code == 401
    # /refresh should return 401
    ref_resp = await client.post("/refresh", json={"refresh_token": refresh_token})
    assert ref_resp.status_code == 401


@pytest.mark.asyncio
async def test_expired_refresh_token_rejected(client):
    """A refresh token with past expires_at should be rejected."""
    await client.post(
        "/register",
        json={"email": "expired@example.com", "username": "expired", "password": "password123"},
    )
    login_resp = await client.post(
        "/login", json={"email": "expired@example.com", "password": "password123"}
    )
    refresh_token = login_resp.json()["refresh_token"]
    # Expire the token in DB
    token_hash = hash_token(refresh_token)
    async with TestSessionLocal() as session:
        from sqlalchemy import update

        await session.execute(
            update(RefreshToken)
            .where(RefreshToken.token_hash == token_hash)
            .values(expires_at=datetime.now(timezone.utc) - timedelta(hours=1))
        )
        await session.commit()
    # Refresh should fail
    resp = await client.post("/refresh", json={"refresh_token": refresh_token})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_register_short_password(client):
    """Password shorter than 8 characters returns 422."""
    resp = await client.post(
        "/register",
        json={"email": "short@example.com", "username": "shortpw", "password": "abc123"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_refresh_with_already_used_token(client):
    """After refresh (token rotation), using the old refresh token again should fail."""
    await client.post(
        "/register",
        json={"email": "reuse@example.com", "username": "reuseuser", "password": "password123"},
    )
    login_resp = await client.post(
        "/login", json={"email": "reuse@example.com", "password": "password123"}
    )
    original_token = login_resp.json()["refresh_token"]
    # First refresh succeeds and rotates the token
    rotate_resp = await client.post("/refresh", json={"refresh_token": original_token})
    assert rotate_resp.status_code == 200
    new_token = rotate_resp.json()["refresh_token"]
    assert new_token != original_token
    # Second use of original token should fail
    retry_resp = await client.post("/refresh", json={"refresh_token": original_token})
    assert retry_resp.status_code == 401


# ---------------------------------------------------------------------------
# ADR 0011 phase 4: manual admin activate/deactivate endpoints.
# ---------------------------------------------------------------------------


async def _register_and_get(client, email: str, username: str) -> dict:
    await client.post(
        "/register",
        json={"email": email, "username": username, "password": "password123"},
    )
    users_resp = await client.get("/users")
    return next(u for u in users_resp.json()["items"] if u["email"] == email)


@pytest.mark.asyncio
async def test_superadmin_can_deactivate_and_reactivate_user(superadmin_client):
    target = await _register_and_get(superadmin_client, "flip@test.com", "flipuser")

    deactivate_resp = await superadmin_client.post(f"/users/{target['id']}/deactivate")
    assert deactivate_resp.status_code == 200
    assert deactivate_resp.json()["is_active"] is False

    activate_resp = await superadmin_client.post(f"/users/{target['id']}/activate")
    assert activate_resp.status_code == 200
    assert activate_resp.json()["is_active"] is True


@pytest.mark.asyncio
async def test_admin_can_deactivate_user(admin_client):
    # admin-or-superadmin, not superadmin-only (unlike the role endpoint).
    target = await _register_and_get(admin_client, "adminflip@test.com", "adminflipuser")
    resp = await admin_client.post(f"/users/{target['id']}/deactivate")
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False


@pytest.mark.asyncio
async def test_deactivate_and_activate_clear_sync_provenance(superadmin_client):
    target = await _register_and_get(superadmin_client, "prov@test.com", "provuser")

    async with TestSessionLocal() as session:
        user = await session.get(User, uuid.UUID(target["id"]))
        user.deactivated_by_sync = True
        await session.commit()

    resp = await superadmin_client.post(f"/users/{target['id']}/deactivate")
    assert resp.status_code == 200

    async with TestSessionLocal() as session:
        user = await session.get(User, uuid.UUID(target["id"]))
        assert user.is_active is False
        # Admin intent always outranks the directory: a manually
        # deactivated user must be invisible to sync-side reactivation.
        assert user.deactivated_by_sync is False

    reactivate_resp = await superadmin_client.post(f"/users/{target['id']}/activate")
    assert reactivate_resp.status_code == 200

    async with TestSessionLocal() as session:
        user = await session.get(User, uuid.UUID(target["id"]))
        assert user.is_active is True
        assert user.deactivated_by_sync is False


@pytest.mark.asyncio
async def test_activate_nonexistent_user_404(superadmin_client):
    resp = await superadmin_client.post(f"/users/{uuid.uuid4()}/activate")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_deactivate_nonexistent_user_404(superadmin_client):
    resp = await superadmin_client.post(f"/users/{uuid.uuid4()}/deactivate")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_cannot_deactivate_own_account(superadmin_client):
    resp = await superadmin_client.post(f"/users/{_superadmin_id}/deactivate")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Cannot deactivate your own account"


@pytest.mark.asyncio
async def test_regular_user_cannot_activate_or_deactivate(regular_client):
    some_id = uuid.uuid4()
    activate_resp = await regular_client.post(f"/users/{some_id}/activate")
    assert activate_resp.status_code == 403
    deactivate_resp = await regular_client.post(f"/users/{some_id}/deactivate")
    assert deactivate_resp.status_code == 403
