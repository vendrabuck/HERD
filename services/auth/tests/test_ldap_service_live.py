"""Live LDAP tests against a running OpenLDAP container.

Skipped automatically when nothing is listening on the configured host/port,
so the suite stays green on CI without the container. To run locally, start
the checked-in test directory (`make ldap-up`, which boots infra/ldap-test
seeded with the fixtures asserted here) and rerun pytest.

Setting HERD_TEST_LDAP_REQUIRED=1 disables the skip: an unreachable server
then fails every test with an explicit message instead. The master and
everything gates set it (via `make test-auth-ldap`) so these tests can never
silently skip inside a gate run.

Expected directory layout:

    dc=company,dc=local
        ou=people
            uid=user1..user6   (mail=userN@company.local, password=Password1)
            uid=nomail1        (no mail attribute)
            cn=nouid1          (mail but no uid attribute)
        ou=groups              (groupOfNames; see 60-seed-groups.ldif)
            cn=herd-eng        (user1..user3)
            cn=herd-qa         (user4, user5)
            cn=herd-mixed      (user6, nomail1, nouid1)
            cn=herd-stale      (user6 plus a nonexistent member DN)

Bind DN for the service account: cn=admin,dc=company,dc=local / admin.
"""

from __future__ import annotations

import os
import socket

import pytest
from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models.user import User
from app.services import auth_service, ldap_service
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

LDAP_HOST = os.getenv("HERD_TEST_LDAP_HOST", "127.0.0.1")
LDAP_PORT = int(os.getenv("HERD_TEST_LDAP_PORT", "389"))
LDAP_URL = f"ldap://{LDAP_HOST}:{LDAP_PORT}"
BASE_DN = "dc=company,dc=local"
PEOPLE_DN = f"ou=people,{BASE_DN}"
ADMIN_DN = f"cn=admin,{BASE_DN}"
ADMIN_PW = "admin"
USER_PW = "Password1"


def _ldap_reachable() -> bool:
    try:
        with socket.create_connection((LDAP_HOST, LDAP_PORT), timeout=1.0):
            return True
    except OSError:
        return False


_LDAP_REQUIRED = os.getenv("HERD_TEST_LDAP_REQUIRED", "") not in ("", "0")
_LDAP_REACHABLE = _ldap_reachable()

pytestmark = pytest.mark.skipif(
    not _LDAP_REQUIRED and not _LDAP_REACHABLE,
    reason=f"No LDAP server reachable at {LDAP_URL}; start the test directory to run.",
)


@pytest.fixture(autouse=True)
def _fail_when_required_but_unreachable():
    if _LDAP_REQUIRED and not _LDAP_REACHABLE:
        pytest.fail(
            f"HERD_TEST_LDAP_REQUIRED is set but no LDAP server is reachable at {LDAP_URL}; "
            "run `make ldap-up` (infra/ldap-test) or unset HERD_TEST_LDAP_REQUIRED."
        )


@pytest.fixture
def ldap_settings(monkeypatch):
    """Point settings at the live osixia/openldap container."""
    monkeypatch.setattr(settings, "auth_method", "ldap", raising=False)
    monkeypatch.setattr(settings, "ldap_server_url", LDAP_URL, raising=False)
    monkeypatch.setattr(settings, "ldap_bind_dn", ADMIN_DN, raising=False)
    monkeypatch.setattr(settings, "ldap_bind_password", ADMIN_PW, raising=False)
    monkeypatch.setattr(settings, "ldap_user_base_dn", PEOPLE_DN, raising=False)
    monkeypatch.setattr(settings, "ldap_user_filter", "(uid={username})", raising=False)
    monkeypatch.setattr(settings, "ldap_email_attribute", "mail", raising=False)
    monkeypatch.setattr(settings, "ldap_username_attribute", "uid", raising=False)
    monkeypatch.setattr(settings, "ldap_use_tls", False, raising=False)


@pytest.mark.asyncio
async def test_bind_user_success_returns_identity(ldap_settings):
    identity = await ldap_service.bind_user("user1", USER_PW)
    assert identity is not None
    assert identity.username == "user1"
    assert identity.email == "user1@company.local"
    assert identity.dn.lower() == f"uid=user1,{PEOPLE_DN}".lower()


@pytest.mark.asyncio
async def test_bind_user_wrong_password_returns_none(ldap_settings):
    assert await ldap_service.bind_user("user1", "wrong-password") is None


@pytest.mark.asyncio
async def test_bind_user_unknown_user_returns_none(ldap_settings):
    assert await ldap_service.bind_user("does-not-exist", USER_PW) is None


@pytest.mark.asyncio
async def test_bind_user_empty_password_rejected(ldap_settings):
    # Anonymous bind would otherwise succeed against many directories; the
    # service must short-circuit before issuing the second bind.
    assert await ldap_service.bind_user("user1", "") is None


@pytest.mark.asyncio
async def test_bind_user_filter_metacharacters_are_escaped(ldap_settings):
    # `*` is an LDAP filter wildcard; without escaping this would match every
    # user in the OU. The service must escape it and find no one.
    assert await ldap_service.bind_user("user*", USER_PW) is None


@pytest.mark.asyncio
async def test_bad_service_account_returns_none(monkeypatch, ldap_settings):
    monkeypatch.setattr(settings, "ldap_bind_password", "wrong-admin-pw", raising=False)
    assert await ldap_service.bind_user("user1", USER_PW) is None


# ---------------------------------------------------------------------------
# End-to-end through authenticate_user with a SQLite session, exercising the
# JIT provisioning path against a real directory.
# ---------------------------------------------------------------------------


@pytest.fixture
async def db_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def db_session(db_engine):
    Session = async_sessionmaker(db_engine, expire_on_commit=False)
    async with Session() as session:
        yield session


@pytest.mark.asyncio
async def test_authenticate_user_jit_provisions_from_live_ldap(ldap_settings, db_session):
    user = await auth_service.authenticate_user(db_session, "user2", USER_PW)
    assert user is not None
    assert user.email == "user2@company.local"
    assert user.username == "user2"
    assert user.auth_source == "ldap"
    assert user.hashed_password is None

    # A second login must reuse the row, not duplicate it.
    await auth_service.authenticate_user(db_session, "user2", USER_PW)
    rows = (
        (await db_session.execute(select(User).where(User.email == "user2@company.local")))
        .scalars()
        .all()
    )
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_authenticate_user_rejects_local_account_with_same_email(ldap_settings, db_session):
    # A pre-existing local account with the email LDAP would return must not
    # be silently taken over by the LDAP backend.
    await auth_service.create_user(db_session, "user3@company.local", "user3-local", "localpw123")
    assert await auth_service.authenticate_user(db_session, "user3", USER_PW) is None


# ---------------------------------------------------------------------------
# /login HTTP surface against the real directory.
# ---------------------------------------------------------------------------


@pytest.fixture
async def http_client(db_engine):
    Session = async_sessionmaker(db_engine, expire_on_commit=False)

    async def _override():
        async with Session() as session:
            yield session

    app.dependency_overrides[get_db] = _override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_login_endpoint_succeeds_against_live_ldap(ldap_settings, http_client):
    resp = await http_client.post("/login", json={"email": "user4", "password": USER_PW})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "access_token" in body


@pytest.mark.asyncio
async def test_login_endpoint_rejects_bad_password_against_live_ldap(ldap_settings, http_client):
    resp = await http_client.post("/login", json={"email": "user4", "password": "nope"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# ADR 0011 group-sync directory client (fixtures in 60-seed-groups.ldif).
# The load-bearing contract under test: None / skip_reason are answers the
# directory actually gave; LdapUnavailableError is raised whenever it could
# not be asked, so fail-closed consumers never mistake an outage for absence.
# ---------------------------------------------------------------------------

GROUPS_DN = f"ou=groups,{BASE_DN}"


@pytest.mark.asyncio
async def test_fetch_group_returns_name_and_member_dns(ldap_settings):
    group = await ldap_service.fetch_group(f"cn=herd-eng,{GROUPS_DN}")
    assert group is not None
    assert group.name == "herd-eng"
    members = {dn.lower() for dn in group.member_dns}
    assert members == {f"uid=user{n},{PEOPLE_DN}".lower() for n in (1, 2, 3)}


@pytest.mark.asyncio
async def test_fetch_group_nonexistent_dn_is_dangling_none(ldap_settings):
    assert await ldap_service.fetch_group(f"cn=renamed-away,{GROUPS_DN}") is None


@pytest.mark.asyncio
async def test_fetch_group_bad_service_account_raises(monkeypatch, ldap_settings):
    monkeypatch.setattr(settings, "ldap_bind_password", "wrong-admin-pw", raising=False)
    with pytest.raises(ldap_service.LdapUnavailableError):
        await ldap_service.fetch_group(f"cn=herd-eng,{GROUPS_DN}")


@pytest.mark.asyncio
async def test_fetch_group_unreachable_server_raises(monkeypatch, ldap_settings):
    # A closed port refuses the connection; that is an outage, never a
    # dangling mapping.
    monkeypatch.setattr(
        settings, "ldap_server_url", f"ldap://{LDAP_HOST}:{LDAP_PORT + 10000}", raising=False
    )
    with pytest.raises(ldap_service.LdapUnavailableError):
        await ldap_service.fetch_group(f"cn=herd-eng,{GROUPS_DN}")


@pytest.mark.asyncio
async def test_resolve_member_returns_identity(ldap_settings):
    resolution = await ldap_service.resolve_member(f"uid=user1,{PEOPLE_DN}")
    assert resolution.skip_reason is None
    assert resolution.identity is not None
    assert resolution.identity.email == "user1@company.local"
    assert resolution.identity.username == "user1"


@pytest.mark.asyncio
async def test_resolve_member_nonexistent_dn_is_proven_not_found(ldap_settings):
    # herd-stale lists this DN as a member even though no entry exists; the
    # directory answers noSuchObject, which is proof, not an error.
    resolution = await ldap_service.resolve_member(f"uid=ghost1,{PEOPLE_DN}")
    assert resolution.identity is None
    assert resolution.skip_reason == ldap_service.MEMBER_SKIP_NOT_FOUND


@pytest.mark.asyncio
async def test_resolve_member_without_email_is_skipped(ldap_settings):
    resolution = await ldap_service.resolve_member(f"uid=nomail1,{PEOPLE_DN}")
    assert resolution.identity is None
    assert resolution.skip_reason == ldap_service.MEMBER_SKIP_MISSING_EMAIL


@pytest.mark.asyncio
async def test_resolve_member_without_username_is_skipped(ldap_settings):
    resolution = await ldap_service.resolve_member(f"cn=nouid1,{PEOPLE_DN}")
    assert resolution.identity is None
    assert resolution.skip_reason == ldap_service.MEMBER_SKIP_MISSING_USERNAME


@pytest.mark.asyncio
async def test_resolve_member_bad_service_account_raises(monkeypatch, ldap_settings):
    monkeypatch.setattr(settings, "ldap_bind_password", "wrong-admin-pw", raising=False)
    with pytest.raises(ldap_service.LdapUnavailableError):
        await ldap_service.resolve_member(f"uid=user1,{PEOPLE_DN}")


@pytest.mark.asyncio
async def test_user_present_by_email_found(ldap_settings):
    assert await ldap_service.user_present_by_email("user1@company.local") is True


@pytest.mark.asyncio
async def test_user_present_by_email_proven_absent(ldap_settings):
    assert await ldap_service.user_present_by_email("ghost1@company.local") is False


@pytest.mark.asyncio
async def test_user_present_by_email_escapes_filter_metacharacters(ldap_settings):
    # Unescaped, the wildcard would match every seeded user and "prove"
    # presence for an email that belongs to no one.
    assert await ldap_service.user_present_by_email("user*@company.local") is False


@pytest.mark.asyncio
async def test_user_present_by_email_bad_service_account_raises(monkeypatch, ldap_settings):
    monkeypatch.setattr(settings, "ldap_bind_password", "wrong-admin-pw", raising=False)
    with pytest.raises(ldap_service.LdapUnavailableError):
        await ldap_service.user_present_by_email("user1@company.local")


@pytest.mark.asyncio
async def test_user_present_by_email_bad_base_dn_raises_not_absent(monkeypatch, ldap_settings):
    # The asymmetry pin: a base-scope lookup on a missing DN is proof for
    # fetch_group/resolve_member, but a presence search under a missing BASE
    # proves nothing about the user. Reporting False here is exactly the
    # misread that would let a misconfigured base DN mass-deactivate.
    monkeypatch.setattr(settings, "ldap_user_base_dn", f"ou=nowhere,{BASE_DN}", raising=False)
    with pytest.raises(ldap_service.LdapUnavailableError):
        await ldap_service.user_present_by_email("user1@company.local")
