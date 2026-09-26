"""HTTP tests for the effective-role fix, driven with REAL JWTs.

Every other HTTP test file in this suite authorizes through the
get_current_user dependency override installed by tests._harness.mock_user /
conftest.make_client, which never decodes an actual token. That is exactly
right for exercising route bodies, but it cannot exercise the bug this file
is about: the auth service used to authorize against current_user.role (the
bare database role) instead of the JWT's role claim, so a lower-privileged
token minted for a higher-privileged account passed every gate inside this
service. Proving the fix needs a real signed token whose claim can differ
from the account's database role, decoded by the real dependency chain.

make_client() with no argument already leaves get_current_user
un-overridden (see its docstring) while still installing the in-memory DB
override, so it is used directly here as the "real auth" client; only the
mock-user convenience is skipped. Tokens are minted with
app.utils.jwt.create_access_token, the exact function
auth_service.create_tokens_for_user and api_token_service.exchange_api_token
use in production, so the claims here are shaped exactly like a real login
or exchange JWT.
"""

import uuid

import pytest
from app.models.user import Role, User
from app.utils.jwt import create_access_token

from tests._harness import TestSessionLocal

_FORBIDDEN_DETAIL = "You do not have permission to perform this action"
_UNAUTHENTICATED_DETAIL = "Could not validate credentials"


async def _make_user(role: Role, *, username: str, is_active: bool = True) -> User:
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


_OMIT = object()


def _mint(user: User, role_claim=_OMIT) -> str:
    """Mint a real access JWT for `user`.

    role_claim defaults to the user's own database role (the normal,
    unremarkable case: a login JWT always carries the account's role at
    issue time). Pass an explicit Role/str to simulate a token whose claim
    diverges from the database (an API-token exchange that clamped the
    claim below the principal's role, or a demotion since issue). Pass
    None to drop the `role` key from the payload entirely.
    """
    payload = {"sub": str(user.id), "username": user.username, "email": user.email}
    if role_claim is _OMIT:
        payload["role"] = user.role.value
    elif role_claim is not None:
        payload["role"] = role_claim.value if isinstance(role_claim, Role) else role_claim
    # role_claim is None: no "role" key at all.
    return create_access_token(payload)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def real_client(make_client):
    """A client whose requests are authorized end to end through the real
    JWT-decode and effective-role path (get_current_user left un-overridden),
    with only the in-memory DB kept overridden.
    """
    async with make_client() as ac:
        yield ac


@pytest.mark.asyncio
async def test_user_claim_on_superadmin_row_is_refused_everywhere(real_client):
    """(a) A user-claim JWT whose sub is a superadmin row: the reported defect.

    Before the fix, require_role and create_token's checks read
    current_user.role (the database role, superadmin) and every one of
    these passed. All five must now 403 against the claim instead.
    """
    sa = await _make_user(Role.SUPERADMIN, username="sa-case-a")
    headers = _auth(_mint(sa, Role.USER))

    resp = await real_client.get("/users", headers=headers)
    assert resp.status_code == 403
    assert resp.json()["detail"] == _FORBIDDEN_DETAIL

    target = await _make_user(Role.USER, username="target-case-a")
    resp = await real_client.put(
        f"/users/{target.id}/role", headers=headers, json={"role": "admin"}
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == _FORBIDDEN_DETAIL

    resp = await real_client.post("/groups", headers=headers, json={"name": "case-a-group"})
    assert resp.status_code == 403
    assert resp.json()["detail"] == _FORBIDDEN_DETAIL

    resp = await real_client.get("/admin/ldap-sync/status", headers=headers)
    assert resp.status_code == 403
    assert resp.json()["detail"] == _FORBIDDEN_DETAIL

    principal = await _make_user(Role.USER, username="principal-case-a")
    for requested_role in ("admin", "superadmin"):
        resp = await real_client.post(
            "/tokens",
            headers=headers,
            json={"name": "x", "principal_id": str(principal.id), "role": requested_role},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == _FORBIDDEN_DETAIL


@pytest.mark.asyncio
async def test_admin_claim_on_superadmin_row_caps_at_admin(real_client):
    """(b) An admin-claim JWT on a superadmin row can mint admin and user
    tokens but is refused minting a superadmin token."""
    sa = await _make_user(Role.SUPERADMIN, username="sa-case-b")
    headers = _auth(_mint(sa, Role.ADMIN))
    # Two principals whose own database role matches the requested token role,
    # so create_api_token's separate token-cannot-exceed-its-principal check
    # (an unrelated invariant; see api_token_service._role_exceeds) never fires
    # here: this test is only about the CALLER's effective-role ceiling.
    user_principal = await _make_user(Role.USER, username="principal-case-b-user")
    admin_principal = await _make_user(Role.ADMIN, username="principal-case-b-admin")

    for requested_role, principal in (("user", user_principal), ("admin", admin_principal)):
        resp = await real_client.post(
            "/tokens",
            headers=headers,
            json={
                "name": f"mint-{requested_role}",
                "principal_id": str(principal.id),
                "role": requested_role,
            },
        )
        assert resp.status_code == 201, resp.text

    resp = await real_client.post(
        "/tokens",
        headers=headers,
        json={
            "name": "mint-superadmin",
            "principal_id": str(admin_principal.id),
            "role": "superadmin",
        },
    )
    assert resp.status_code == 403
    assert (
        resp.json()["detail"] == "Cannot mint a token whose role or principal exceeds your own role"
    )


@pytest.mark.asyncio
async def test_superadmin_claim_on_superadmin_row_is_unchanged(real_client):
    """(c) A superadmin-claim JWT on a superadmin row passes every gate,
    exactly as before this fix (claim equals database role)."""
    sa = await _make_user(Role.SUPERADMIN, username="sa-case-c")
    headers = _auth(_mint(sa, Role.SUPERADMIN))

    resp = await real_client.get("/users", headers=headers)
    assert resp.status_code == 200

    resp = await real_client.get("/admin/ldap-sync/status", headers=headers)
    assert resp.status_code == 200

    resp = await real_client.post("/groups", headers=headers, json={"name": "case-c-group"})
    assert resp.status_code == 201, resp.text

    # sa is its own principal here: minting a superadmin-role token needs a
    # principal whose OWN database role is at least superadmin too (an
    # unrelated invariant enforced by create_api_token), and sa already is one.
    resp = await real_client.post(
        "/tokens",
        headers=headers,
        json={"name": "mint-superadmin", "principal_id": str(sa.id), "role": "superadmin"},
    )
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_no_role_claim_on_admin_row_is_treated_as_user(real_client):
    """(d) A JWT with no role claim at all on an admin row is held to user."""
    admin = await _make_user(Role.ADMIN, username="admin-case-d")
    headers = _auth(_mint(admin, None))

    resp = await real_client.get("/users", headers=headers)
    assert resp.status_code == 403
    assert resp.json()["detail"] == _FORBIDDEN_DETAIL


@pytest.mark.asyncio
async def test_inactive_account_is_refused_regardless_of_claim(real_client):
    """(e) An inactive account is refused before any role decision, even
    carrying a superadmin claim."""
    sa = await _make_user(Role.SUPERADMIN, username="sa-case-e", is_active=False)
    headers = _auth(_mint(sa, Role.SUPERADMIN))

    resp = await real_client.get("/users", headers=headers)
    assert resp.status_code == 401
    assert resp.json()["detail"] == _UNAUTHENTICATED_DETAIL


@pytest.mark.asyncio
async def test_api_token_exchange_clamp_binds_end_to_end(real_client):
    """The full path of the reported defect, closed: a superadmin mints a
    user-role API token for itself through the real endpoint, exchanges it
    through the real endpoint, and the resulting JWT is held to user
    everywhere in this service, including when it tries to mint a HIGHER
    token with itself.
    """
    sa = await _make_user(Role.SUPERADMIN, username="sa-case-e2e")
    sa_headers = _auth(_mint(sa, Role.SUPERADMIN))

    create_resp = await real_client.post(
        "/tokens",
        headers=sa_headers,
        json={"name": "exploit-probe", "principal_id": str(sa.id), "role": "user"},
    )
    assert create_resp.status_code == 201, create_resp.text
    raw_token = create_resp.json()["token"]

    exchange_resp = await real_client.post("/tokens/exchange", json={"token": raw_token})
    assert exchange_resp.status_code == 200, exchange_resp.text
    exchanged_headers = _auth(exchange_resp.json()["access_token"])

    resp = await real_client.get("/users", headers=exchanged_headers)
    assert resp.status_code == 403
    assert resp.json()["detail"] == _FORBIDDEN_DETAIL

    principal = await _make_user(Role.USER, username="principal-case-e2e")
    resp = await real_client.post(
        "/tokens",
        headers=exchanged_headers,
        json={"name": "escalate", "principal_id": str(principal.id), "role": "admin"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == _FORBIDDEN_DETAIL
