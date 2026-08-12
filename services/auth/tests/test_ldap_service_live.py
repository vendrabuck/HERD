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


def _seed_is_current() -> bool:
    """Probe the last-loaded fixture (cn=herd-stale from 60-seed-groups.ldif).

    The stateless container reseeds only on `up`, so a container started
    from an older checkout answers for user1 while lacking the group
    fixtures; without this probe those runs fail with confusing dangling
    None assertions instead of a clear remedy.
    """
    import ldap3

    try:
        server = ldap3.Server(LDAP_URL, get_info=ldap3.NONE, connect_timeout=5)
        conn = ldap3.Connection(server, user=ADMIN_DN, password=ADMIN_PW, auto_bind=True)
        try:
            ok = conn.search(
                f"cn=herd-stale,ou=groups,{BASE_DN}",
                "(objectClass=*)",
                search_scope=ldap3.BASE,
                attributes=["cn"],
            )
            return bool(ok and conn.entries)
        finally:
            conn.unbind()
    except Exception:
        return False


@pytest.fixture(autouse=True)
def _fail_when_required_but_unreachable():
    if _LDAP_REQUIRED and not _LDAP_REACHABLE:
        pytest.fail(
            f"HERD_TEST_LDAP_REQUIRED is set but no LDAP server is reachable at {LDAP_URL}; "
            "run `make ldap-up` (infra/ldap-test) or unset HERD_TEST_LDAP_REQUIRED."
        )


@pytest.fixture(scope="session", autouse=True)
def _fail_on_stale_seed():
    if _LDAP_REACHABLE and not _seed_is_current():
        pytest.fail(
            f"LDAP server at {LDAP_URL} is reachable but missing the group fixtures "
            "(60-seed-groups.ldif); it was seeded from an older checkout. "
            "Run `make ldap-reset` to reseed."
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
async def test_fetch_group_invalid_dn_syntax_is_proven_unresolvable(ldap_settings):
    # invalidDNSyntax is a stable answer (the DN can never resolve), so it
    # classifies as dangling None, not as a perpetual outage; phase 2
    # mapping validation must 422 this, not 503.
    assert await ldap_service.fetch_group("not-a-dn") is None


_SYNC_CLIENT_CALLS = [
    pytest.param(
        lambda: ldap_service.fetch_group(f"cn=herd-eng,ou=groups,{BASE_DN}"),
        id="fetch_group",
    ),
    pytest.param(
        lambda: ldap_service.resolve_member(f"uid=user1,{PEOPLE_DN}"),
        id="resolve_member",
    ),
    pytest.param(
        lambda: ldap_service.resolve_members([f"uid=user1,{PEOPLE_DN}"]),
        id="resolve_members",
    ),
    pytest.param(
        lambda: ldap_service.user_present_by_email("user1@company.local"),
        id="user_present_by_email",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("call", _SYNC_CLIENT_CALLS)
async def test_sync_client_bad_service_account_raises(monkeypatch, ldap_settings, call):
    monkeypatch.setattr(settings, "ldap_bind_password", "wrong-admin-pw", raising=False)
    with pytest.raises(ldap_service.LdapUnavailableError):
        await call()


@pytest.mark.asyncio
@pytest.mark.parametrize("call", _SYNC_CLIENT_CALLS)
async def test_sync_client_anonymous_bind_refused(monkeypatch, ldap_settings, call):
    # An empty bind DN must raise, not silently fall back to an anonymous
    # bind: ACL-denied anonymous reads answer noSuchObject, which would
    # convert a credentials misconfiguration into false proven absence.
    monkeypatch.setattr(settings, "ldap_bind_dn", "", raising=False)
    with pytest.raises(ldap_service.LdapUnavailableError):
        await call()


@pytest.mark.asyncio
async def test_fetch_group_unreachable_server_raises(monkeypatch, ldap_settings):
    # A refused connection is an outage, never a dangling mapping. The port
    # is allocated fresh (bound then released) so nothing else can be
    # listening on it, unlike a hardcoded offset from LDAP_PORT.
    with socket.socket() as probe:
        probe.bind((LDAP_HOST, 0))
        refused_port = probe.getsockname()[1]
    monkeypatch.setattr(
        settings, "ldap_server_url", f"ldap://{LDAP_HOST}:{refused_port}", raising=False
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
@pytest.mark.parametrize(
    ("member_rdn", "expected_reason"),
    [
        pytest.param("uid=ghost1", ldap_service.MEMBER_SKIP_NOT_FOUND, id="nonexistent-dn"),
        pytest.param("uid=nomail1", ldap_service.MEMBER_SKIP_MISSING_EMAIL, id="no-email"),
        pytest.param("cn=nouid1", ldap_service.MEMBER_SKIP_MISSING_USERNAME, id="no-username"),
    ],
)
async def test_resolve_member_skip_reasons(ldap_settings, member_rdn, expected_reason):
    # ghost1 is listed as a herd-stale member with no entry: the directory
    # answers noSuchObject, which is proof, not an error. The attribute
    # cases are entries that exist but lack what JIT provisioning needs.
    resolution = await ldap_service.resolve_member(f"{member_rdn},{PEOPLE_DN}")
    assert resolution.identity is None
    assert resolution.skip_reason == expected_reason


@pytest.mark.asyncio
async def test_resolve_members_batch_over_one_connection(ldap_settings):
    # The reconciler-facing batch: herd-mixed's membership resolves to one
    # identity plus both attribute-skip cases.
    group = await ldap_service.fetch_group(f"cn=herd-mixed,{GROUPS_DN}")
    assert group is not None
    resolutions = await ldap_service.resolve_members(group.member_dns)
    assert len(resolutions) == 3
    assert {r.skip_reason for r in resolutions} == {
        None,
        ldap_service.MEMBER_SKIP_MISSING_EMAIL,
        ldap_service.MEMBER_SKIP_MISSING_USERNAME,
    }
    resolved = [r.identity for r in resolutions if r.identity is not None]
    assert [i.email for i in resolved] == ["user6@company.local"]


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
async def test_user_present_by_email_bad_base_dn_raises_not_absent(monkeypatch, ldap_settings):
    # The asymmetry pin: a base-scope lookup on a missing DN is proof for
    # fetch_group/resolve_member, but a presence search under a missing BASE
    # proves nothing about the user. Reporting False here is exactly the
    # misread that would let a misconfigured base DN mass-deactivate.
    monkeypatch.setattr(settings, "ldap_user_base_dn", f"ou=nowhere,{BASE_DN}", raising=False)
    with pytest.raises(ldap_service.LdapUnavailableError):
        await ldap_service.user_present_by_email("user1@company.local")
