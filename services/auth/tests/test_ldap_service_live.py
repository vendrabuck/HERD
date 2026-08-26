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
            uid=user1..user6      (mail=userN@company.local, password=Password1)
            uid=nomail1           (no mail attribute)
            cn=nouid1             (mail but no uid attribute)
            uid=ldapit-admin      (see 70-seed-integration.ldif)
            uid=ldapit-eng1..3    (see 70-seed-integration.ldif)
        ou=groups              (groupOfNames; see 60-seed-groups.ldif)
            cn=herd-eng        (user1..user3)
            cn=herd-qa         (user4, user5)
            cn=herd-mixed      (user6, nomail1, nouid1)
            cn=herd-stale      (user6 plus a nonexistent member DN)
            cn=herd-it-eng     (ldapit-eng1..3; see 70-seed-integration.ldif,
                                issue #572, the dedicated identities
                                tests/integration/test_ldap_sync_admin.py
                                uses so it never collides with a seeded
                                stack's local user1..user1000 rows)

Bind DN for the service account: cn=admin,dc=company,dc=local / admin.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import uuid

import ldap3
import pytest
from app import database
from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models.group import GroupMember, UserGroup
from app.models.ldap_group_mapping import LdapGroupMapping
from app.models.ldap_sync_run import LdapSyncRun
from app.models.user import User
from app.services import auth_service, ldap_service, ldap_sync_service
from app.tasks import ldap_sync_loop as loop_module
from httpx import ASGITransport, AsyncClient
from ldap3.core.exceptions import LDAPNoSuchObjectResult
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
    """Probe the last-loaded fixture of each fixture-adding LDIF file:
    cn=herd-stale (60-seed-groups.ldif) and cn=herd-it-eng
    (70-seed-integration.ldif, issue #572, added after herd-stale).

    The stateless container reseeds only on `up`, so a container started
    from an older checkout answers for user1 while lacking one or both sets
    of group fixtures; without this probe those runs fail with confusing
    dangling None assertions (or a silently-wrong membership assertion)
    instead of a clear remedy. Checking only the single newest file would
    miss a checkout stale relative to 60-seed-groups.ldif but current as of
    an even older commit that already had 70-seed-integration.ldif; checking
    both catches either gap.
    """
    import ldap3

    try:
        server = ldap3.Server(LDAP_URL, get_info=ldap3.NONE, connect_timeout=5)
        conn = ldap3.Connection(server, user=ADMIN_DN, password=ADMIN_PW, auto_bind=True)
        try:
            for cn in ("herd-stale", "herd-it-eng"):
                ok = conn.search(
                    f"cn={cn},ou=groups,{BASE_DN}",
                    "(objectClass=*)",
                    search_scope=ldap3.BASE,
                    attributes=["cn"],
                )
                if not (ok and conn.entries):
                    return False
            return True
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
            "(60-seed-groups.ldif and/or 70-seed-integration.ldif); it was seeded "
            "from an older checkout. Run `make ldap-reset` to reseed."
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
async def db_engine(use_reap_session_factory):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # run_sync reaps stale runs on its OWN session (issue #528), which by
    # default comes from app.database rather than this private engine. Point
    # it here so the reap sees the rows these tests create instead of a
    # database with no ldap_sync_runs table at all.
    use_reap_session_factory(async_sessionmaker(engine, expire_on_commit=False))
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
async def test_fetch_group_on_non_group_entry_reports_no_members(ldap_settings):
    # fetch_group proves existence, not group-ness: an OU resolves like a
    # group with zero members (ldap3 back-fills the missing member attribute
    # as empty, so emptiness is the only observable signal). This is the
    # typo'd-mapping case phase 2's accept-with-warning rule exists for,
    # pinned against a real directory.
    group = await ldap_service.fetch_group(PEOPLE_DN)
    assert group is not None
    assert group.member_dns == ()


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


# ---------------------------------------------------------------------------
# ADR 0011 phase 3: the reconciler (ldap_sync_service.run_sync) against the
# REAL directory, not a faked one. The unit suite (test_ldap_sync_service.py)
# pins the taxonomy against a fake client; what only a live run can prove is
# that the real ldap_service.fetch_group/resolve_members answers plug into
# that taxonomy correctly for the seeded groups (see the module docstring
# above for the exact membership of herd-eng/qa/mixed/stale).
# ---------------------------------------------------------------------------


async def _mk_herd_group(db_session, name: str) -> uuid.UUID:
    group = UserGroup(name=name)
    db_session.add(group)
    await db_session.commit()
    return group.id


async def _mk_mapping(
    db_session, *, group_dn: str, herd_group_id: uuid.UUID, directory_name: str
) -> LdapGroupMapping:
    mapping = LdapGroupMapping(
        group_dn=group_dn, directory_name=directory_name, herd_group_id=herd_group_id
    )
    db_session.add(mapping)
    await db_session.commit()
    return mapping


async def _member_emails(db_session, group_id: uuid.UUID) -> set[str]:
    rows = await db_session.execute(
        select(User.email)
        .join(GroupMember, GroupMember.user_id == User.id)
        .where(GroupMember.group_id == group_id)
    )
    return set(rows.scalars().all())


@pytest.mark.asyncio
async def test_run_sync_herd_eng_builds_membership_from_live_directory(ldap_settings, db_session):
    group_id = await _mk_herd_group(db_session, "Engineering")
    await _mk_mapping(
        db_session,
        group_dn=f"cn=herd-eng,{GROUPS_DN}",
        herd_group_id=group_id,
        directory_name="herd-eng",
    )

    run = await ldap_sync_service.run_sync(db_session)

    assert run.status == "success"
    assert run.users_provisioned == 3
    assert run.members_added == 3
    assert run.members_removed == 0
    assert run.members_skipped == 0

    emails = await _member_emails(db_session, group_id)
    assert emails == {f"user{n}@company.local" for n in (1, 2, 3)}

    provisioned = (
        (
            await db_session.execute(
                select(User).where(User.email.in_(f"user{n}@company.local" for n in (1, 2, 3)))
            )
        )
        .scalars()
        .all()
    )
    assert len(provisioned) == 3
    for user in provisioned:
        assert user.auth_source == "ldap"
        assert user.hashed_password is None


@pytest.mark.asyncio
async def test_run_sync_second_run_is_idempotent(ldap_settings, db_session):
    group_id = await _mk_herd_group(db_session, "Engineering")
    await _mk_mapping(
        db_session,
        group_dn=f"cn=herd-eng,{GROUPS_DN}",
        herd_group_id=group_id,
        directory_name="herd-eng",
    )

    first = await ldap_sync_service.run_sync(db_session)
    assert first.status == "success"
    assert first.members_added == 3

    second = await ldap_sync_service.run_sync(db_session)

    assert second.status == "success"
    assert second.users_provisioned == 0
    assert second.members_added == 0
    assert second.members_removed == 0
    assert second.members_skipped == 0

    emails = await _member_emails(db_session, group_id)
    assert emails == {f"user{n}@company.local" for n in (1, 2, 3)}


@pytest.mark.asyncio
async def test_run_sync_herd_mixed_counts_exact_member_skips(ldap_settings, db_session):
    group_id = await _mk_herd_group(db_session, "Mixed")
    await _mk_mapping(
        db_session,
        group_dn=f"cn=herd-mixed,{GROUPS_DN}",
        herd_group_id=group_id,
        directory_name="herd-mixed",
    )

    run = await ldap_sync_service.run_sync(db_session)

    assert run.status == "partial"
    assert run.members_skipped == 2
    assert run.users_provisioned == 1
    assert run.members_added == 1

    skip_reasons = {r["reason"] for r in run.detail.get("skipped_members", [])}
    assert skip_reasons == {
        ldap_service.MEMBER_SKIP_MISSING_EMAIL,
        ldap_service.MEMBER_SKIP_MISSING_USERNAME,
    }

    emails = await _member_emails(db_session, group_id)
    assert emails == {"user6@company.local"}


@pytest.mark.asyncio
async def test_run_sync_herd_stale_ghost_member_skipped_not_found(ldap_settings, db_session):
    group_id = await _mk_herd_group(db_session, "Stale")
    await _mk_mapping(
        db_session,
        group_dn=f"cn=herd-stale,{GROUPS_DN}",
        herd_group_id=group_id,
        directory_name="herd-stale",
    )

    run = await ldap_sync_service.run_sync(db_session)

    assert run.status == "partial"
    assert run.members_skipped == 1
    assert run.users_provisioned == 1
    assert run.members_added == 1

    skipped = run.detail.get("skipped_members", [])
    assert len(skipped) == 1
    assert skipped[0]["reason"] == ldap_service.MEMBER_SKIP_NOT_FOUND

    emails = await _member_emails(db_session, group_id)
    assert emails == {"user6@company.local"}


@pytest.mark.asyncio
async def test_run_sync_dangling_mapping_applies_nothing(ldap_settings, db_session):
    # A pre-existing member must survive untouched: a dangling group DN is
    # unknowable membership, never an empty one, so the reconciler must
    # never drive a removal against it.
    group_id = await _mk_herd_group(db_session, "Ghosts")
    survivor_user = await auth_service.create_ldap_user(db_session, "user5@company.local", "user5")
    db_session.add(GroupMember(group_id=group_id, user_id=survivor_user.id))
    await db_session.commit()

    await _mk_mapping(
        db_session,
        group_dn=f"cn=renamed-away,{GROUPS_DN}",
        herd_group_id=group_id,
        directory_name="renamed-away",
    )

    run = await ldap_sync_service.run_sync(db_session)

    assert run.status == "partial"
    assert run.users_provisioned == 0
    assert run.members_added == 0
    assert run.members_removed == 0
    assert run.members_skipped == 0

    skipped_groups = run.detail.get("skipped_groups", [])
    assert len(skipped_groups) == 1
    assert skipped_groups[0]["reason"] == ldap_sync_service.GROUP_SKIP_DANGLING_DN
    assert skipped_groups[0]["group_dn"] == f"cn=renamed-away,{GROUPS_DN}"

    emails = await _member_emails(db_session, group_id)
    assert emails == {"user5@company.local"}


# ---------------------------------------------------------------------------
# ADR 0011 phase 4: paged presence enumeration and the deactivation and
# reactivation sweep, against the real directory. Fixtures added at test
# runtime through an admin ldap3 connection (never the checked-in LDIF),
# always cleaned up in a finally so the directory ends exactly as seeded.
# ---------------------------------------------------------------------------

# All 25 seeded people (50-seed-people.ldif) plus nouid1 (mail but no uid,
# 60-seed-groups.ldif) plus the four ldapit-* identities (mail but no uid
# quirk does not apply to them, 70-seed-integration.ldif, issue #572) carry a
# mail attribute; nomail1 deliberately does not.
_ALL_SEEDED_PRESENT_EMAILS = frozenset(
    {f"user{n}@company.local" for n in range(1, 26)}
    | {"nouid1@company.local"}
    | {
        "ldapit-admin@company.local",
        "ldapit-eng1@company.local",
        "ldapit-eng2@company.local",
        "ldapit-eng3@company.local",
    }
)


@pytest.mark.asyncio
async def test_present_emails_live_returns_all_seeded_emails(ldap_settings):
    assert await ldap_service.present_emails() == _ALL_SEEDED_PRESENT_EMAILS


@pytest.mark.asyncio
async def test_disabled_emails_live_filter_matching_nothing_returns_empty(
    monkeypatch, ldap_settings
):
    # A syntactically valid, always-false filter (a real attribute, an
    # email no seeded entry has): proves the conjoined search runs cleanly
    # against a real directory and correctly proves zero matches, distinct
    # from an unreachable-directory raise.
    monkeypatch.setattr(
        settings,
        "ldap_disabled_filter",
        "(mail=nobody-matches-this@nowhere.invalid)",
        raising=False,
    )
    assert await ldap_service.disabled_emails() == frozenset()


def _admin_conn() -> ldap3.Connection:
    server = ldap3.Server(LDAP_URL, get_info=ldap3.NONE, connect_timeout=5)
    return ldap3.Connection(server, user=ADMIN_DN, password=ADMIN_PW, auto_bind=True)


def _safe_delete(conn: ldap3.Connection, dn: str) -> None:
    """Delete an entry, tolerating "already gone" so cleanup is idempotent
    regardless of which phase of the test failed."""
    try:
        conn.delete(dn)
    except LDAPNoSuchObjectResult:
        pass


@pytest.mark.asyncio
async def test_live_deactivation_and_reactivation_sweep(monkeypatch, ldap_settings, db_session):
    """Full lifecycle against the real directory, in one test so each phase
    builds on the last: (1) a temp person provisioned via a temp mapped
    group, sweep deactivates no one; (2) the person is deleted from the
    directory and removed from the group, a second run deactivates them
    (deactivated_by_sync True); (3) the entry and membership are restored, a
    third run reactivates them (provenance cleared). The temp group also
    carries user1 as a second member throughout, both because groupOfNames
    requires a nonempty member attribute (so removing the temp person alone
    never leaves a schema-invalid zero-member group) and so the breaker's
    "swept" denominator is not just the one row: with the default
    min_count=3, one absence out of two candidates is still comfortably
    under the floor, which is what "applies rather than aborts" is
    demonstrating here (an artificially large candidate pool would not
    change that outcome, since min_count, not percent, is the binding term
    at this scale).
    """
    monkeypatch.setattr(settings, "ldap_sync_deactivation_enabled", True, raising=False)

    temp_person_dn = f"uid=sweep-tmp-1,{PEOPLE_DN}"
    temp_person_email = "sweep-tmp-1@company.local"
    temp_group_dn = f"cn=sweep-tmp-group,{GROUPS_DN}"
    user1_dn = f"uid=user1,{PEOPLE_DN}"

    conn = _admin_conn()
    try:
        conn.add(
            temp_person_dn,
            attributes={
                "objectClass": ["inetOrgPerson"],
                "uid": "sweep-tmp-1",
                "cn": "Sweep Tmp1",
                "sn": "Tmp1",
                "mail": temp_person_email,
                "userPassword": "Password1",
            },
        )
        assert conn.result["result"] == 0, conn.result
        conn.add(
            temp_group_dn,
            attributes={
                "objectClass": ["groupOfNames"],
                "cn": "sweep-tmp-group",
                "member": [temp_person_dn, user1_dn],
            },
        )
        assert conn.result["result"] == 0, conn.result

        group_id = await _mk_herd_group(db_session, "Sweep Tmp")
        await _mk_mapping(
            db_session,
            group_dn=temp_group_dn,
            herd_group_id=group_id,
            directory_name="sweep-tmp-group",
        )

        # --- Phase 1: full run provisions the temp user via the group; the
        # sweep (now enabled) deactivates no one, everyone is present. ---
        run1 = await ldap_sync_service.run_sync(db_session)
        assert run1.status == "success"
        assert run1.users_deactivated == 0
        assert run1.users_reactivated == 0
        emails = await _member_emails(db_session, group_id)
        assert emails == {temp_person_email, "user1@company.local"}

        temp_user = await auth_service.get_user_by_email(db_session, temp_person_email)
        assert temp_user is not None
        assert temp_user.is_active is True

        # --- Phase 2: drop the person from the group, then delete the
        # entry (user1 stays, so the group keeps a valid nonempty member
        # attribute); a second run proves absence and deactivates. The
        # modify must precede the delete: this server's MDB backend
        # answers "no such value" for a MODIFY_DELETE naming a member DN
        # whose entry was already removed, even though a read-back of the
        # group still lists that exact value (confirmed empirically against
        # the live container; not a documented ldap3 or slapd behavior this
        # test relies on beyond "the safe order is modify-then-delete"). ---
        conn.modify(temp_group_dn, {"member": [(ldap3.MODIFY_DELETE, [temp_person_dn])]})
        assert conn.result["result"] == 0, conn.result
        _safe_delete(conn, temp_person_dn)

        run2 = await ldap_sync_service.run_sync(db_session)
        assert run2.status != "aborted"
        assert run2.users_deactivated == 1
        assert run2.users_reactivated == 0

        await db_session.refresh(temp_user)
        assert temp_user.is_active is False
        assert temp_user.deactivated_by_sync is True

        # --- Phase 3: restore the entry and re-add it to the group; a
        # third run proves presence again and reactivates. ---
        conn.add(
            temp_person_dn,
            attributes={
                "objectClass": ["inetOrgPerson"],
                "uid": "sweep-tmp-1",
                "cn": "Sweep Tmp1",
                "sn": "Tmp1",
                "mail": temp_person_email,
                "userPassword": "Password1",
            },
        )
        assert conn.result["result"] == 0, conn.result
        conn.modify(temp_group_dn, {"member": [(ldap3.MODIFY_ADD, [temp_person_dn])]})
        assert conn.result["result"] == 0, conn.result

        run3 = await ldap_sync_service.run_sync(db_session)
        assert run3.users_reactivated == 1

        await db_session.refresh(temp_user)
        assert temp_user.is_active is True
        assert temp_user.deactivated_by_sync is False
    finally:
        # The directory must end exactly as seeded regardless of which
        # phase above failed.
        _safe_delete(conn, temp_group_dn)
        _safe_delete(conn, temp_person_dn)
        conn.unbind()


# ---------------------------------------------------------------------------
# ADR 0011 phase 5: the interval loop (app/tasks/ldap_sync_loop.py) against
# the REAL directory. Everything above proves ldap_sync_service.run_sync
# itself is correct; what only a live run of the actual LOOP FUNCTION can
# prove is that the phase 5 wiring around it, trigger="interval", the
# sleep-before-first-tick ordering, and driving run_sync through the same
# _SyncSlot serialization sync-now uses, works end-to-end and produces a
# real audit row.
#
# database.AsyncSessionLocal is patched to a FILE-BACKED temp sqlite engine
# (tmp_path, not the shared in-memory db_engine fixture used elsewhere in
# this file): a first version of this test shared one :memory: engine
# (single StaticPool connection) between the loop's own sessions and this
# test's polling sessions, and that was empirically reproduced discarding
# in-flight writes under concurrent access from the two coroutines. A
# separate on-disk database file gives the loop and the poller independent
# connections that both see the same committed data without contending for
# one shared connection object. The interval is forced to a fraction of a
# second (not an env var: passing interval_seconds directly is the cleaner
# seam, since ldap_sync_loop already accepts it as a parameter for exactly
# this reason) so the loop completes a real tick well inside the test's
# timeout instead of the production default of an hour.
#
# Polling is EXISTENCE-based, not earliest-row-based: it waits for any
# finished run with trigger "interval" and members_added == 3 (the group
# build), rather than assuming the first chronological "interval" row is
# that one. started_at is second-granularity, so a slow poll racing a second
# (idempotent, members_added == 0) tick could tie or invert the visible
# order; asserting on content instead of position sidesteps that entirely,
# and idempotent no-op ticks are free to exist alongside without failing
# anything.
# ---------------------------------------------------------------------------


async def _poll_for_run_with_members_added(
    session_factory: async_sessionmaker, *, trigger: str, members_added: int, timeout: float = 5.0
) -> LdapSyncRun:
    """Poll for ANY finished (non-"running") ldap_sync_runs row matching
    trigger and members_added, using a fresh session per attempt so each
    poll sees the latest committed state rather than a stale snapshot from a
    long-lived session."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while True:
        async with session_factory() as s:
            row = (
                (
                    await s.execute(
                        select(LdapSyncRun).where(
                            LdapSyncRun.trigger == trigger,
                            LdapSyncRun.status != "running",
                            LdapSyncRun.members_added == members_added,
                        )
                    )
                )
                .scalars()
                .first()
            )
        if row is not None:
            return row
        if loop.time() >= deadline:
            raise AssertionError(
                f"no finished '{trigger}' run with members_added == {members_added} "
                f"appeared within {timeout}s"
            )
        await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_interval_loop_produces_a_run_row_against_live_directory(
    monkeypatch, ldap_settings, tmp_path
):
    db_path = tmp_path / "ldap_sync_loop_live_test.db"
    file_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
    async with file_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    try:
        session_factory = async_sessionmaker(file_engine, expire_on_commit=False)
        monkeypatch.setattr(database, "AsyncSessionLocal", session_factory)

        async with session_factory() as db:
            group_id = await _mk_herd_group(db, "Interval Loop")
            await _mk_mapping(
                db,
                group_dn=f"cn=herd-eng,{GROUPS_DN}",
                herd_group_id=group_id,
                directory_name="herd-eng",
            )

        loop_task = asyncio.create_task(loop_module.ldap_sync_loop(0.2))
        try:
            run = await _poll_for_run_with_members_added(
                session_factory, trigger="interval", members_added=3
            )
        finally:
            loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await loop_task

        assert run.status == "success"

        async with session_factory() as db:
            emails = await _member_emails(db, group_id)
        assert emails == {f"user{n}@company.local" for n in (1, 2, 3)}
    finally:
        await file_engine.dispose()
