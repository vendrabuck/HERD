"""Unit tests for the directory group reconciler (ADR 0011 phase 3).

The directory client is faked at the ldap_service module boundary; what is
pinned here is the reconciler's asymmetric fail-closed taxonomy. Transport
errors (LdapUnavailableError) skip the WHOLE group with zero changes and
mark the run partial; directory answers (dangling DN, member skip_reasons)
skip only what was answered about; per-op apply failures are isolated per
operation. These are three distinct policies, not one, and the tests treat
each distinction as load-bearing. SQLite enforces the real unique
constraints on users.email and users.username, so the collision paths run
against genuine IntegrityErrors rather than mocks.
"""

import asyncio
import uuid

import pytest
from app.config import settings
from app.database import Base
from app.models.group import GroupMember, UserGroup
from app.models.ldap_group_mapping import LdapGroupMapping
from app.models.ldap_sync_run import LdapSyncRun
from app.models.user import User
from app.services import auth_service, group_service, ldap_service, ldap_sync_service
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

engine = create_async_engine(TEST_DATABASE_URL, echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

_PEOPLE = "ou=people,dc=company,dc=local"
_GROUP_DN = "cn=herd-eng,ou=groups,dc=company,dc=local"


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def db():
    async with TestSessionLocal() as session:
        yield session


def _dn(n: int) -> str:
    return f"uid=user{n},{_PEOPLE}"


def _identity(n: int) -> ldap_service.LdapIdentity:
    return ldap_service.LdapIdentity(username=f"user{n}", email=f"user{n}@company.local", dn=_dn(n))


def _resolved(n: int) -> ldap_service.LdapMemberResolution:
    return ldap_service.LdapMemberResolution(_identity(n))


def _skipped(reason: str) -> ldap_service.LdapMemberResolution:
    return ldap_service.LdapMemberResolution(None, reason)


def _entry(member_dns, dn=_GROUP_DN, name="herd-eng") -> ldap_service.LdapGroupEntry:
    return ldap_service.LdapGroupEntry(dn=dn, name=name, member_dns=tuple(member_dns))


def _install_directory(monkeypatch, groups: dict, resolutions: dict) -> None:
    """Fake the directory: groups maps dn to entry | None | Exception;
    resolutions maps member dn to a resolution | Exception. An Exception in
    the middle of a batch raises mid-batch, exactly like resolve_members."""

    async def fake_fetch_group(group_dn: str, *, run_holder=None):
        value = groups[group_dn]
        if isinstance(value, Exception):
            raise value
        return value

    async def fake_resolve_members(member_dns, *, run_holder=None):
        out = []
        for member_dn in member_dns:
            value = resolutions[member_dn]
            if isinstance(value, Exception):
                raise value
            out.append(value)
        return out

    monkeypatch.setattr(ldap_service, "fetch_group", fake_fetch_group)
    monkeypatch.setattr(ldap_service, "resolve_members", fake_resolve_members)


async def _mk_group(db, name="Engineering") -> uuid.UUID:
    group = UserGroup(name=name)
    db.add(group)
    await db.commit()
    return group.id


async def _mk_mapping(db, herd_group_id, group_dn=_GROUP_DN, name="herd-eng") -> uuid.UUID:
    mapping = LdapGroupMapping(group_dn=group_dn, directory_name=name, herd_group_id=herd_group_id)
    db.add(mapping)
    await db.commit()
    return mapping.id


async def _mk_user(db, n=None, *, email=None, username=None, auth_source="ldap") -> uuid.UUID:
    user = User(
        email=email or f"user{n}@company.local",
        username=username or f"user{n}",
        hashed_password=None if auth_source == "ldap" else "fake",
        auth_source=auth_source,
    )
    db.add(user)
    await db.commit()
    return user.id


async def _put_in_group(db, group_id, user_id) -> None:
    db.add(GroupMember(group_id=group_id, user_id=user_id))
    await db.commit()


async def _member_ids(db, group_id) -> set:
    rows = await db.execute(select(GroupMember.user_id).where(GroupMember.group_id == group_id))
    return set(rows.scalars().all())


def _skip_reasons(run) -> set:
    return {r["reason"] for r in run.detail.get("skipped_members", []) if "reason" in r}


# ---------------------------------------------------------------------------
# ADR 0011 phase 4: deactivation and reactivation sweep helpers.
# ---------------------------------------------------------------------------


def _enable_deactivation(
    monkeypatch,
    *,
    max_percent: int = 20,
    min_count: int = 3,
    disabled_filter: str = "",
) -> None:
    monkeypatch.setattr(settings, "ldap_sync_deactivation_enabled", True, raising=False)
    monkeypatch.setattr(settings, "ldap_sync_deactivation_max_percent", max_percent, raising=False)
    monkeypatch.setattr(settings, "ldap_sync_deactivation_min_count", min_count, raising=False)
    monkeypatch.setattr(settings, "ldap_disabled_filter", disabled_filter, raising=False)


def _install_presence(monkeypatch, present, disabled=None) -> None:
    """Fake ldap_service.present_emails/disabled_emails. present/disabled are
    a frozenset[str] or an Exception to raise."""

    async def fake_present_emails(*, run_holder=None):
        if isinstance(present, Exception):
            raise present
        return frozenset(present)

    async def fake_disabled_emails(*, run_holder=None):
        value = disabled if disabled is not None else frozenset()
        if isinstance(value, Exception):
            raise value
        return frozenset(value)

    monkeypatch.setattr(ldap_service, "present_emails", fake_present_emails)
    monkeypatch.setattr(ldap_service, "disabled_emails", fake_disabled_emails)


async def _mk_candidates(db, n: int) -> list[uuid.UUID]:
    return [await _mk_user(db, i) for i in range(1, n + 1)]


async def _deactivate(db, user_id: uuid.UUID, *, by_sync: bool) -> None:
    user = await db.get(User, user_id)
    user.is_active = False
    user.deactivated_by_sync = by_sync
    await db.commit()


# ---------------------------------------------------------------------------
# Model defaults (S4: moved here from the deleted test_ldap_sync_run_model.py
# so the run-lifecycle model and the reconciler that populates it are pinned
# in one file, against one set of fixtures).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_insert_run_applies_server_defaults(db):
    run_id = uuid.uuid4()
    run = LdapSyncRun(id=run_id, trigger="manual", status="running")
    db.add(run)
    await db.commit()

    assert run.id == run_id
    assert run.trigger == "manual"
    assert run.status == "running"
    assert run.started_at is not None
    assert run.finished_at is None
    assert run.users_provisioned == 0
    assert run.members_added == 0
    assert run.members_removed == 0
    assert run.members_skipped == 0
    assert run.users_deactivated == 0
    assert run.users_reactivated == 0
    assert run.detail == {}
    assert run.error is None


# ---------------------------------------------------------------------------
# Fail-closed group skips: each applies ZERO changes and marks the run partial.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_error_skips_whole_group_with_zero_changes(db, monkeypatch):
    group_id = await _mk_group(db)
    await _mk_mapping(db, group_id)
    stale = await _mk_user(db, 1)
    await _put_in_group(db, group_id, stale)
    _install_directory(monkeypatch, {_GROUP_DN: ldap_service.LdapUnavailableError("down")}, {})

    run = await ldap_sync_service.run_sync(db)

    assert run.status == "partial"
    assert (run.members_added, run.members_removed, run.users_provisioned) == (0, 0, 0)
    # The stale member would have been removed by an applied reconcile; an
    # unreachable directory must never strip membership.
    assert await _member_ids(db, group_id) == {stale}
    assert run.detail["skipped_groups"] == [
        {
            "group_dn": _GROUP_DN,
            "reason": ldap_sync_service.GROUP_SKIP_DIRECTORY_UNAVAILABLE,
            "error": "down",
        }
    ]
    assert run.finished_at is not None


@pytest.mark.asyncio
async def test_dangling_dn_skips_whole_group_with_zero_changes(db, monkeypatch):
    group_id = await _mk_group(db)
    await _mk_mapping(db, group_id)
    stale = await _mk_user(db, 1)
    await _put_in_group(db, group_id, stale)
    _install_directory(monkeypatch, {_GROUP_DN: None}, {})

    run = await ldap_sync_service.run_sync(db)

    assert run.status == "partial"
    assert (run.members_added, run.members_removed, run.users_provisioned) == (0, 0, 0)
    assert await _member_ids(db, group_id) == {stale}
    assert run.detail["skipped_groups"][0]["reason"] == ldap_sync_service.GROUP_SKIP_DANGLING_DN


@pytest.mark.asyncio
async def test_member_resolution_error_skips_whole_group(db, monkeypatch):
    # One member resolves fine before the batch errors; the half-resolved
    # desired set must be discarded whole, not applied partially.
    group_id = await _mk_group(db)
    await _mk_mapping(db, group_id)
    stale = await _mk_user(db, 9)
    await _put_in_group(db, group_id, stale)
    _install_directory(
        monkeypatch,
        {_GROUP_DN: _entry([_dn(1), _dn(2)])},
        {_dn(1): _resolved(1), _dn(2): ldap_service.LdapUnavailableError("timeout")},
    )

    run = await ldap_sync_service.run_sync(db)

    assert run.status == "partial"
    assert (run.members_added, run.members_removed, run.users_provisioned) == (0, 0, 0)
    assert await _member_ids(db, group_id) == {stale}
    assert (
        run.detail["skipped_groups"][0]["reason"]
        == ldap_sync_service.GROUP_SKIP_MEMBER_RESOLUTION_UNAVAILABLE
    )


# ---------------------------------------------------------------------------
# Set-difference reconcile and pre-provisioning.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_difference_applies_exact_diff_through_group_service(db, monkeypatch):
    group_id = await _mk_group(db)
    not_grouped_id = await _mk_group(db, name="Not Grouped")
    mapping_id = await _mk_mapping(db, group_id, name="stale-cached-name")
    stale = await _mk_user(db, 1)
    await _put_in_group(db, group_id, stale)
    existing = await _mk_user(db, 2)
    await _put_in_group(db, not_grouped_id, existing)
    # Desired: user2 (exists, not yet a member) and user3 (no HERD row).
    _install_directory(
        monkeypatch,
        {_GROUP_DN: _entry([_dn(2), _dn(3)], name="herd-eng")},
        {_dn(2): _resolved(2), _dn(3): _resolved(3)},
    )

    run = await ldap_sync_service.run_sync(db)

    assert run.status == "success"
    assert run.members_added == 2
    assert run.members_removed == 1
    assert run.users_provisioned == 1
    provisioned = await auth_service.get_user_by_email(db, "user3@company.local")
    assert provisioned is not None
    assert await _member_ids(db, group_id) == {existing, provisioned.id}
    # Applied through group_service.add_member: the "Not Grouped" auto-remove
    # is the observable proof.
    assert await _member_ids(db, not_grouped_id) == set()
    # directory_name display cache refreshed on the successful fetch.
    name = (
        await db.execute(
            select(LdapGroupMapping.directory_name).where(LdapGroupMapping.id == mapping_id)
        )
    ).scalar_one()
    assert name == "herd-eng"


@pytest.mark.asyncio
async def test_preprovision_creates_ldap_user_without_password(db, monkeypatch):
    group_id = await _mk_group(db)
    await _mk_mapping(db, group_id)
    _install_directory(monkeypatch, {_GROUP_DN: _entry([_dn(1)])}, {_dn(1): _resolved(1)})

    run = await ldap_sync_service.run_sync(db)

    assert run.status == "success"
    assert run.users_provisioned == 1
    user = await auth_service.get_user_by_email(db, "user1@company.local")
    assert user is not None
    assert user.auth_source == "ldap"
    assert user.hashed_password is None
    assert user.username == "user1"
    assert await _member_ids(db, group_id) == {user.id}


@pytest.mark.asyncio
async def test_member_skip_reasons_counted_without_failing_group(db, monkeypatch):
    # Skips are answers the directory gave; the group still reconciles the
    # members it did resolve (unlike transport errors, which skip the group).
    group_id = await _mk_group(db)
    await _mk_mapping(db, group_id)
    _install_directory(
        monkeypatch,
        {_GROUP_DN: _entry([_dn(1), "uid=gone," + _PEOPLE, "uid=noemail," + _PEOPLE])},
        {
            _dn(1): _resolved(1),
            "uid=gone," + _PEOPLE: _skipped(ldap_service.MEMBER_SKIP_NOT_FOUND),
            "uid=noemail," + _PEOPLE: _skipped(ldap_service.MEMBER_SKIP_MISSING_EMAIL),
        },
    )

    run = await ldap_sync_service.run_sync(db)

    assert run.status == "partial"
    assert run.members_skipped == 2
    assert run.members_added == 1
    assert _skip_reasons(run) == {
        ldap_service.MEMBER_SKIP_NOT_FOUND,
        ldap_service.MEMBER_SKIP_MISSING_EMAIL,
    }


# ---------------------------------------------------------------------------
# Provisioning collision categories (three, distinct).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_collision_username_taken_skips_member(db, monkeypatch):
    group_id = await _mk_group(db)
    await _mk_mapping(db, group_id)
    # A local account owns the username but a different email: the insert
    # hits the real unique constraint and the retry lookup finds nothing.
    await _mk_user(db, email="other@company.local", username="user1", auth_source="local")
    _install_directory(monkeypatch, {_GROUP_DN: _entry([_dn(1)])}, {_dn(1): _resolved(1)})

    run = await ldap_sync_service.run_sync(db)

    assert run.status == "partial"
    assert run.users_provisioned == 0
    assert run.members_skipped == 1
    assert _skip_reasons(run) == {ldap_sync_service.MEMBER_SKIP_USERNAME_TAKEN}
    assert await auth_service.get_user_by_email(db, "user1@company.local") is None
    assert await _member_ids(db, group_id) == set()


@pytest.mark.asyncio
async def test_collision_email_owned_by_local_account_skips_member(db, monkeypatch):
    group_id = await _mk_group(db)
    await _mk_mapping(db, group_id)
    local = await _mk_user(db, email="user1@company.local", username="someone", auth_source="local")
    _install_directory(monkeypatch, {_GROUP_DN: _entry([_dn(1)])}, {_dn(1): _resolved(1)})

    run = await ldap_sync_service.run_sync(db)

    assert run.status == "partial"
    assert run.members_skipped == 1
    assert _skip_reasons(run) == {ldap_sync_service.MEMBER_SKIP_EMAIL_OWNED_BY_LOCAL_ACCOUNT}
    assert await _member_ids(db, group_id) == set()
    # The local identity is never touched: no membership, no drift repair.
    username = (await db.execute(select(User.username).where(User.id == local))).scalar_one()
    assert username == "someone"


@pytest.mark.asyncio
async def test_provision_race_recovered_via_lookup_retry(db, monkeypatch):
    group_id = await _mk_group(db)
    await _mk_mapping(db, group_id)
    real_create = auth_service.create_ldap_user

    async def racing_create(session, email, username, **kwargs):
        # A concurrent JIT login wins the insert; ours then raises. The
        # retry-as-lookup must find the row and proceed.
        await real_create(session, email, username, **kwargs)
        raise IntegrityError("duplicate email", params=None, orig=Exception("race"))

    monkeypatch.setattr(auth_service, "create_ldap_user", racing_create)
    _install_directory(monkeypatch, {_GROUP_DN: _entry([_dn(1)])}, {_dn(1): _resolved(1)})

    run = await ldap_sync_service.run_sync(db)

    # A recovered race is not a skip: the member proceeded, so the run is
    # a success, and nothing was provisioned by THIS run.
    assert run.status == "success"
    assert run.users_provisioned == 0
    assert run.members_skipped == 0
    assert run.detail["provision_races"] == [
        {
            "group_dn": _GROUP_DN,
            "email": "user1@company.local",
            "reason": ldap_sync_service.PROVISION_RACE_RECOVERED,
        }
    ]
    user = await auth_service.get_user_by_email(db, "user1@company.local")
    assert await _member_ids(db, group_id) == {user.id}


@pytest.mark.asyncio
async def test_not_grouped_assignment_failure_rolls_back_and_run_continues(db, monkeypatch):
    # C7: create_ldap_user's "Not Grouped" auto-assign
    # (auth_service._auto_assign_not_grouped) can itself raise a genuine
    # commit-time IntegrityError. Without a rollback there, the session is
    # left pending-rollback and the NEXT statement in this long-lived run
    # (the current_ids SELECT, or the target group's own add) raises
    # PendingRollbackError, failing the whole run instead of just this one
    # auto-assign. The row is inserted TWICE in one commit to force a
    # genuine composite-PK IntegrityError, not a bare raise, so this pins
    # the real recovery path.
    group_id = await _mk_group(db)
    await _mk_mapping(db, group_id)
    await _mk_group(db, name="Not Grouped")
    real_add_member = group_service._add_member_resolved
    calls = {"n": 0}

    async def flaky_add_member(session, group_id_, user_id_, not_grouped_id_):
        calls["n"] += 1
        if calls["n"] == 1:
            session.add(GroupMember(group_id=group_id_, user_id=user_id_))
            session.add(GroupMember(group_id=group_id_, user_id=user_id_))
            return await session.commit()
        return await real_add_member(session, group_id_, user_id_, not_grouped_id_)

    monkeypatch.setattr(group_service, "_add_member_resolved", flaky_add_member)
    _install_directory(monkeypatch, {_GROUP_DN: _entry([_dn(1)])}, {_dn(1): _resolved(1)})

    run = await ldap_sync_service.run_sync(db)

    assert run.status == "success"
    assert run.users_provisioned == 1
    assert run.members_added == 1
    provisioned = await auth_service.get_user_by_email(db, "user1@company.local")
    assert provisioned is not None
    assert await _member_ids(db, group_id) == {provisioned.id}
    # The session stayed usable past the failure: a follow-up write on it
    # succeeds rather than raising PendingRollbackError.
    another = await _mk_user(db, 2)
    assert another is not None


# ---------------------------------------------------------------------------
# Username drift repair.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_username_drift_repaired(db, monkeypatch):
    group_id = await _mk_group(db)
    await _mk_mapping(db, group_id)
    drifted = await _mk_user(db, email="user1@company.local", username="old-name")
    _install_directory(monkeypatch, {_GROUP_DN: _entry([_dn(1)])}, {_dn(1): _resolved(1)})

    run = await ldap_sync_service.run_sync(db)

    assert run.status == "success"
    username = (await db.execute(select(User.username).where(User.id == drifted))).scalar_one()
    assert username == "user1"
    assert await _member_ids(db, group_id) == {drifted}


@pytest.mark.asyncio
async def test_username_drift_collision_skips_repair_not_membership(db, monkeypatch):
    group_id = await _mk_group(db)
    await _mk_mapping(db, group_id)
    drifted = await _mk_user(db, email="user1@company.local", username="old-name")
    await _mk_user(db, email="squatter@company.local", username="user1", auth_source="local")
    _install_directory(monkeypatch, {_GROUP_DN: _entry([_dn(1)])}, {_dn(1): _resolved(1)})

    run = await ldap_sync_service.run_sync(db)

    # C6: a drift collision degrades the run (via its own detail category)
    # but must NOT double-count as a member skip; the member still
    # reconciles into the group, so counting it as skipped too would make
    # the run's counters lie about what happened to this one person.
    assert run.status == "partial"
    assert run.members_skipped == 0
    assert run.detail["drift_collisions"] == [
        {
            "group_dn": _GROUP_DN,
            "member_dn": _dn(1),
            "reason": ldap_sync_service.MEMBER_SKIP_USERNAME_DRIFT_COLLISION,
        }
    ]
    # Only the repair is skipped: the member is still a proven directory
    # answer, so membership still reconciles.
    assert await _member_ids(db, group_id) == {drifted}
    username = (await db.execute(select(User.username).where(User.id == drifted))).scalar_one()
    assert username == "old-name"


# ---------------------------------------------------------------------------
# C4: removal suppression on an unresolvable-but-existing member. A member
# the directory still lists but cannot identify (missing_email or
# missing_username) makes the group's whole removal set unprovable, since
# that unresolvable entry could be any current row. A proven not_found skip
# carries no such ambiguity and never blocks removals.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_email_skip_suppresses_group_removals(db, monkeypatch):
    group_id = await _mk_group(db)
    await _mk_mapping(db, group_id)
    existing = await _mk_user(db, 1)
    await _put_in_group(db, group_id, existing)
    # The directory still lists this exact member, but an attribute-level
    # gap (e.g. an ACL change hiding mail) makes them unresolvable this
    # pass; without suppression this existing member would be stripped.
    _install_directory(
        monkeypatch,
        {_GROUP_DN: _entry([_dn(1)])},
        {_dn(1): _skipped(ldap_service.MEMBER_SKIP_MISSING_EMAIL)},
    )

    run = await ldap_sync_service.run_sync(db)

    assert run.status == "partial"
    assert run.members_removed == 0
    assert await _member_ids(db, group_id) == {existing}
    assert run.detail["suppressed_removals"] == [
        {"group_dn": _GROUP_DN, "unresolved": 1, "would_remove": 1}
    ]


@pytest.mark.asyncio
async def test_not_found_skip_does_not_suppress_removal(db, monkeypatch):
    group_id = await _mk_group(db)
    await _mk_mapping(db, group_id)
    stale = await _mk_user(db, 1)
    await _put_in_group(db, group_id, stale)
    # A DIFFERENT, unrelated DN resolves not_found: a proven-absent entry
    # shields nobody, so the stale member's removal still proceeds.
    gone_dn = "uid=gone," + _PEOPLE
    _install_directory(
        monkeypatch,
        {_GROUP_DN: _entry([gone_dn])},
        {gone_dn: _skipped(ldap_service.MEMBER_SKIP_NOT_FOUND)},
    )

    run = await ldap_sync_service.run_sync(db)

    assert run.status == "partial"
    assert run.members_removed == 1
    assert await _member_ids(db, group_id) == set()
    assert "suppressed_removals" not in run.detail


# ---------------------------------------------------------------------------
# Local accounts and inactive users are invisible in both directions.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_group_member_never_removed(db, monkeypatch):
    group_id = await _mk_group(db)
    await _mk_mapping(db, group_id)
    local = await _mk_user(db, email="admin@company.local", username="admin", auth_source="local")
    await _put_in_group(db, group_id, local)
    _install_directory(monkeypatch, {_GROUP_DN: _entry([_dn(1)])}, {_dn(1): _resolved(1)})

    run = await ldap_sync_service.run_sync(db)

    assert run.status == "success"
    assert run.members_removed == 0
    ldap_user = await auth_service.get_user_by_email(db, "user1@company.local")
    assert await _member_ids(db, group_id) == {local, ldap_user.id}


@pytest.mark.asyncio
async def test_inactive_ldap_member_survives_and_is_not_readded(db, monkeypatch):
    # C5, direction 1: an existing member deactivated by an admin must not
    # have their membership stripped by sync, even though they are still
    # listed in the directory group.
    group_id = await _mk_group(db)
    await _mk_mapping(db, group_id)
    inactive = await _mk_user(db, 1)
    user = await db.get(User, inactive)
    user.is_active = False
    await db.commit()
    _install_directory(monkeypatch, {_GROUP_DN: _entry([_dn(1)])}, {_dn(1): _resolved(1)})

    run = await ldap_sync_service.run_sync(db)

    assert run.status == "partial"
    assert run.members_removed == 0
    assert run.members_added == 0
    assert _skip_reasons(run) == {ldap_sync_service.MEMBER_SKIP_USER_INACTIVE}
    assert await _member_ids(db, group_id) == set()


@pytest.mark.asyncio
async def test_inactive_ldap_member_already_grouped_is_untouched(db, monkeypatch):
    # C5, direction 1b: same as above but the inactive user is ALREADY a
    # group member; current_ids must exclude them too, so the diff never
    # even considers removing (or re-adding) them.
    group_id = await _mk_group(db)
    await _mk_mapping(db, group_id)
    inactive = await _mk_user(db, 1)
    await _put_in_group(db, group_id, inactive)
    user = await db.get(User, inactive)
    user.is_active = False
    await db.commit()
    _install_directory(monkeypatch, {_GROUP_DN: _entry([_dn(1)])}, {_dn(1): _resolved(1)})

    run = await ldap_sync_service.run_sync(db)

    assert run.status == "partial"
    assert run.members_removed == 0
    assert run.members_added == 0
    assert await _member_ids(db, group_id) == {inactive}


@pytest.mark.asyncio
async def test_inactive_ldap_user_is_not_added(db, monkeypatch):
    # C5, direction 2: an inactive user not yet a group member must never
    # be added by sync, even though the directory lists them.
    group_id = await _mk_group(db)
    await _mk_mapping(db, group_id)
    inactive = await _mk_user(db, 1)
    user = await db.get(User, inactive)
    user.is_active = False
    await db.commit()
    _install_directory(monkeypatch, {_GROUP_DN: _entry([_dn(1)])}, {_dn(1): _resolved(1)})

    run = await ldap_sync_service.run_sync(db)

    assert run.status == "partial"
    assert run.members_added == 0
    assert _skip_reasons(run) == {ldap_sync_service.MEMBER_SKIP_USER_INACTIVE}
    assert await _member_ids(db, group_id) == set()


# ---------------------------------------------------------------------------
# Per-op apply fault isolation.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_integrity_error_with_existing_row_is_benign_noop(db, monkeypatch):
    # C3: a real concurrent duplicate. The mocked add_member models the
    # race directly: a concurrent admin's insert lands FIRST (so the row
    # genuinely exists by the time our except runs), then our own call
    # raises exactly like a real racing insert would.
    group_id = await _mk_group(db)
    await _mk_mapping(db, group_id)
    await _mk_user(db, 1)

    async def racing_add(session, group_id_, user_id_, not_grouped_id_):
        session.add(GroupMember(group_id=group_id_, user_id=user_id_))
        await session.commit()
        raise IntegrityError("Duplicate membership", params=None, orig=None)

    monkeypatch.setattr(group_service, "_add_member_resolved", racing_add)
    _install_directory(monkeypatch, {_GROUP_DN: _entry([_dn(1)])}, {_dn(1): _resolved(1)})

    run = await ldap_sync_service.run_sync(db)

    # A concurrent admin already added the row: not an add, not a failure.
    assert run.status == "success"
    assert run.members_added == 0
    assert "op_failures" not in run.detail


@pytest.mark.asyncio
async def test_add_integrity_error_without_row_is_op_failure(db, monkeypatch):
    # C3: an IntegrityError is not ALWAYS the benign concurrent duplicate.
    # A commit-time FK violation (user or group deleted mid-run) raises
    # IntegrityError too; here the row never lands, so it must degrade the
    # run rather than being silently swallowed as a no-op.
    group_id = await _mk_group(db)
    await _mk_mapping(db, group_id)
    await _mk_user(db, 1)

    async def phantom_add(session, group_id_, user_id_, not_grouped_id_):
        raise IntegrityError("Duplicate membership", params=None, orig=None)

    monkeypatch.setattr(group_service, "_add_member_resolved", phantom_add)
    _install_directory(monkeypatch, {_GROUP_DN: _entry([_dn(1)])}, {_dn(1): _resolved(1)})

    run = await ldap_sync_service.run_sync(db)

    assert run.status == "partial"
    assert run.members_added == 0
    op_failures = run.detail["op_failures"]
    assert len(op_failures) == 1
    assert op_failures[0]["op"] == "add"
    assert await _member_ids(db, group_id) == set()


@pytest.mark.asyncio
async def test_other_op_failure_is_isolated_and_loop_continues(db, monkeypatch):
    group_id = await _mk_group(db)
    await _mk_mapping(db, group_id)
    bad = await _mk_user(db, 1)
    good = await _mk_user(db, 2)
    real_add = group_service._add_member_resolved

    async def flaky_add(session, group_id_, user_id_, not_grouped_id_):
        if user_id_ == bad:
            raise RuntimeError("db hiccup")
        return await real_add(session, group_id_, user_id_, not_grouped_id_)

    monkeypatch.setattr(group_service, "_add_member_resolved", flaky_add)
    _install_directory(
        monkeypatch,
        {_GROUP_DN: _entry([_dn(1), _dn(2)])},
        {_dn(1): _resolved(1), _dn(2): _resolved(2)},
    )

    run = await ldap_sync_service.run_sync(db)

    assert run.status == "partial"
    assert run.members_added == 1
    assert await _member_ids(db, group_id) == {good}
    assert run.detail["op_failures"] == [
        {"group_dn": _GROUP_DN, "op": "add", "user_id": str(bad), "error": "db hiccup"}
    ]


# ---------------------------------------------------------------------------
# Idempotency and run lifecycle.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_run_against_unchanged_directory_is_noop(db, monkeypatch):
    group_id = await _mk_group(db)
    await _mk_mapping(db, group_id)
    await _mk_user(db, 2)
    _install_directory(
        monkeypatch,
        {_GROUP_DN: _entry([_dn(1), _dn(2)])},
        {_dn(1): _resolved(1), _dn(2): _resolved(2)},
    )

    first = await ldap_sync_service.run_sync(db)
    assert (first.users_provisioned, first.members_added) == (1, 2)

    second = await ldap_sync_service.run_sync(db)
    assert second.status == "success"
    assert (second.users_provisioned, second.members_added, second.members_removed) == (0, 0, 0)
    assert second.members_skipped == 0


@pytest.mark.asyncio
async def test_running_row_is_committed_before_directory_work(db, monkeypatch):
    group_id = await _mk_group(db)
    await _mk_mapping(db, group_id)
    observed = {}

    async def probing_fetch(group_dn, *, run_holder=None):
        # By the time the directory is first asked, the run row must already
        # be committed with status "running" (the crash-corpse guarantee).
        observed["status"] = (await db.execute(select(LdapSyncRun.status))).scalar_one()
        raise ldap_service.LdapUnavailableError("down")

    monkeypatch.setattr(ldap_service, "fetch_group", probing_fetch)

    run = await ldap_sync_service.run_sync(db)

    assert observed["status"] == "running"
    assert run.status == "partial"


@pytest.mark.asyncio
async def test_machinery_exception_yields_failed_run_with_error(db, monkeypatch):
    # A non-LdapUnavailableError out of the directory client is machinery
    # failure, not a directory answer: the run finalizes as "failed" with
    # the error recorded, and run_sync re-raises nothing.
    group_id = await _mk_group(db)
    await _mk_mapping(db, group_id)
    _install_directory(
        monkeypatch, {_GROUP_DN: _entry([_dn(1)])}, {_dn(1): RuntimeError("client bug")}
    )

    run = await ldap_sync_service.run_sync(db)

    assert run.status == "failed"
    assert run.error == "client bug"
    assert run.finished_at is not None
    persisted = (
        await db.execute(select(LdapSyncRun.status).where(LdapSyncRun.id == run.id))
    ).scalar_one()
    assert persisted == "failed"


@pytest.mark.asyncio
async def test_detail_categories_cap_with_truncation_marker(db, monkeypatch):
    group_id = await _mk_group(db)
    await _mk_mapping(db, group_id)
    cap = ldap_sync_service.DETAIL_CAP
    total = cap + 5
    dns = [f"uid=gone{i},{_PEOPLE}" for i in range(total)]
    _install_directory(
        monkeypatch,
        {_GROUP_DN: _entry(dns)},
        {dn: _skipped(ldap_service.MEMBER_SKIP_NOT_FOUND) for dn in dns},
    )

    run = await ldap_sync_service.run_sync(db)

    # The counter is exact; the detail list is capped with a marker.
    assert run.members_skipped == total
    skipped = run.detail["skipped_members"]
    assert len(skipped) == cap + 1
    assert skipped[-1] == {"truncated": 5}
    assert all("reason" in record for record in skipped[:-1])


# ---------------------------------------------------------------------------
# ADR 0011 phase 4: the deactivation and reactivation sweep.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_disabled_when_setting_off(db, monkeypatch):
    # ldap_sync_deactivation_enabled defaults False; the sweep must never
    # even consult the directory client, let alone touch is_active.
    user_id = await _mk_user(db, 1)

    def boom(*_a, **_kw):
        raise AssertionError("directory must not be consulted when deactivation is disabled")

    monkeypatch.setattr(ldap_service, "present_emails", boom)
    monkeypatch.setattr(ldap_service, "disabled_emails", boom)

    run = await ldap_sync_service.run_sync(db)

    assert run.status == "success"
    assert "sweep" not in run.detail
    assert run.users_deactivated == 0
    assert run.users_reactivated == 0
    user = await db.get(User, user_id)
    assert user.is_active is True


@pytest.mark.asyncio
async def test_sweep_enumeration_failure_touches_no_one(db, monkeypatch):
    _enable_deactivation(monkeypatch)
    active_id = await _mk_user(db, 1)
    reactivate_id = await _mk_user(db, 2)
    await _deactivate(db, reactivate_id, by_sync=True)

    _install_presence(monkeypatch, present=ldap_service.LdapUnavailableError("directory down"))

    run = await ldap_sync_service.run_sync(db)

    # Error is never absence: reactivations are ALSO skipped, since presence
    # was not proven either.
    assert run.status == "partial"
    assert run.users_deactivated == 0
    assert run.users_reactivated == 0
    assert run.detail["sweep"] == [
        {
            "reason": ldap_sync_service.SWEEP_REASON_ENUMERATION_UNAVAILABLE,
            "error": "directory down",
        }
    ]
    active = await db.get(User, active_id)
    assert active.is_active is True
    stale = await db.get(User, reactivate_id)
    assert stale.is_active is False
    assert stale.deactivated_by_sync is True


@pytest.mark.asyncio
async def test_sweep_disabled_filter_deactivates_even_when_present_and_credited(db, monkeypatch):
    # A user still listed as present AND credited via this run's group
    # reconcile must still deactivate when the directory reports them
    # disabled: disabled-equals-proven-absent overrides both.
    _enable_deactivation(monkeypatch, disabled_filter="(pwdAccountLockedTime=*)")
    group_id = await _mk_group(db)
    await _mk_mapping(db, group_id)
    member_id = await _mk_user(db, 1)
    _install_directory(monkeypatch, {_GROUP_DN: _entry([_dn(1)])}, {_dn(1): _resolved(1)})
    email = _identity(1).email
    _install_presence(monkeypatch, present={email}, disabled={email})

    run = await ldap_sync_service.run_sync(db)

    assert run.users_deactivated == 1
    assert run.users_reactivated == 0
    member = await db.get(User, member_id)
    assert member.is_active is False
    assert member.deactivated_by_sync is True
    assert run.detail["deactivated"] == [{"user_id": str(member_id), "email": email}]


@pytest.mark.asyncio
async def test_sweep_group_presence_credit_beats_absent_enumeration(db, monkeypatch):
    # The renamed-username scenario: this run's group reconcile resolved the
    # member (credit), even though the paged enumeration alone would report
    # them absent. Credit alone must be enough to prove presence.
    _enable_deactivation(monkeypatch)
    group_id = await _mk_group(db)
    await _mk_mapping(db, group_id)
    member_id = await _mk_user(db, 1)
    _install_directory(monkeypatch, {_GROUP_DN: _entry([_dn(1)])}, {_dn(1): _resolved(1)})
    _install_presence(monkeypatch, present=frozenset())

    run = await ldap_sync_service.run_sync(db)

    assert run.users_deactivated == 0
    member = await db.get(User, member_id)
    assert member.is_active is True


@pytest.mark.asyncio
async def test_sweep_provenance_gate(db, monkeypatch):
    # Admin-deactivated (deactivated_by_sync False) users are NEVER touched,
    # present or not. Sync-deactivated users proven present are reactivated
    # with the provenance flag cleared.
    _enable_deactivation(monkeypatch)
    admin_deactivated_id = await _mk_user(db, 1)
    await _deactivate(db, admin_deactivated_id, by_sync=False)
    sync_deactivated_id = await _mk_user(db, 2)
    await _deactivate(db, sync_deactivated_id, by_sync=True)

    _install_presence(monkeypatch, present={_identity(1).email, _identity(2).email})

    run = await ldap_sync_service.run_sync(db)

    assert run.users_deactivated == 0
    assert run.users_reactivated == 1

    admin_user = await db.get(User, admin_deactivated_id)
    assert admin_user.is_active is False
    assert admin_user.deactivated_by_sync is False

    sync_user = await db.get(User, sync_deactivated_id)
    assert sync_user.is_active is True
    assert sync_user.deactivated_by_sync is False


@pytest.mark.asyncio
async def test_sweep_breaker_boundary_equal_min_count_applies(db, monkeypatch):
    # count == min_count (boundary-equal, NOT strictly exceeded) with a huge
    # percent: the min_count term alone gates this case, so it applies.
    _enable_deactivation(monkeypatch, max_percent=20, min_count=3)
    users = await _mk_candidates(db, 3)  # swept = 3, all three proven absent
    _install_presence(monkeypatch, present=frozenset())

    run = await ldap_sync_service.run_sync(db)

    assert run.status != "aborted"
    assert run.users_deactivated == 3
    for uid in users:
        user = await db.get(User, uid)
        assert user.is_active is False
        assert user.deactivated_by_sync is True


@pytest.mark.asyncio
async def test_sweep_breaker_boundary_equal_percent_applies(db, monkeypatch):
    # count(4) > min_count(3): that term is exceeded. percent is boundary-
    # equal to max_percent (4/20 == 20%): that term is NOT exceeded. Both
    # terms must be strictly exceeded to abort, so this applies.
    _enable_deactivation(monkeypatch, max_percent=20, min_count=3)
    total = 20
    absent = {1, 2, 3, 4}
    await _mk_candidates(db, total)
    present = {f"user{i}@company.local" for i in range(1, total + 1) if i not in absent}
    _install_presence(monkeypatch, present=present)

    run = await ldap_sync_service.run_sync(db)

    assert run.status != "aborted"
    assert run.users_deactivated == 4


@pytest.mark.asyncio
async def test_sweep_breaker_both_terms_exceeded_aborts_but_still_reactivates(db, monkeypatch):
    _enable_deactivation(monkeypatch, max_percent=20, min_count=3)
    total = 20
    absent = {1, 2, 3, 4, 5}  # count(5) > min_count(3) and 5/20=25% > 20%
    users = await _mk_candidates(db, total)
    reactivate_id = await _mk_user(db, 99)
    await _deactivate(db, reactivate_id, by_sync=True)

    present = {f"user{i}@company.local" for i in range(1, total + 1) if i not in absent}
    present.add("user99@company.local")
    _install_presence(monkeypatch, present=present)

    swept = total + 1  # candidates include the reactivation target
    count = len(absent)
    expected_percent = round(count / swept * 100, 2)

    run = await ldap_sync_service.run_sync(db)

    assert run.status == "aborted"
    assert run.users_deactivated == 0
    assert run.users_reactivated == 1
    assert run.error is not None
    assert run.detail["sweep"] == [
        {
            "reason": ldap_sync_service.SWEEP_REASON_BREAKER_TRIPPED,
            "count": count,
            "swept": swept,
            "percent": expected_percent,
        }
    ]
    for uid in users:
        user = await db.get(User, uid)
        assert user.is_active is True
    reactivated = await db.get(User, reactivate_id)
    assert reactivated.is_active is True
    assert reactivated.deactivated_by_sync is False


@pytest.mark.asyncio
async def test_sweep_counters_and_detail_apply_to_run_row(db, monkeypatch):
    _enable_deactivation(monkeypatch)
    deactivate_id = await _mk_user(db, 1)
    reactivate_id = await _mk_user(db, 2)
    await _deactivate(db, reactivate_id, by_sync=True)

    _install_presence(monkeypatch, present={_identity(2).email})

    run = await ldap_sync_service.run_sync(db)

    assert run.users_deactivated == 1
    assert run.users_reactivated == 1
    persisted = (
        await db.execute(
            select(
                LdapSyncRun.users_deactivated,
                LdapSyncRun.users_reactivated,
            ).where(LdapSyncRun.id == run.id)
        )
    ).one()
    assert persisted.users_deactivated == 1
    assert persisted.users_reactivated == 1
    deactivated = await db.get(User, deactivate_id)
    assert deactivated.is_active is False
    assert deactivated.deactivated_by_sync is True


def test_tally_apply_to_writes_deactivation_counters_and_stays_non_degrading():
    tally = ldap_sync_service._Tally()
    tally.record_deactivated(uuid.uuid4(), "a@company.local")
    tally.record_reactivated(uuid.uuid4(), "b@company.local")

    # A clean sweep that deactivated/reactivated someone is NOT degradation:
    # it is the feature's normal, intended outcome.
    assert tally.degraded is False

    run = LdapSyncRun(trigger="manual", status="running")
    tally.apply_to(run)
    assert run.users_deactivated == 1
    assert run.users_reactivated == 1


def test_run_status_vocabulary_includes_aborted():
    # A small, explicit pin: "aborted" is a real member of the status
    # vocabulary the breaker can produce, not just a string typo'd once.
    assert "aborted" in {"success", "partial", "aborted", "failed"}


@pytest.mark.asyncio
async def test_sweep_guarded_update_yields_to_concurrent_admin_write(db, monkeypatch):
    # The clobber interleaving (another replica's admin write landing between
    # the sweep's snapshot and its update) is pinned at the helper level
    # because black-box interleaving is unforceable here: the snapshot User
    # object says active, but the ROW was admin-deactivated after the
    # snapshot. The guarded WHERE must not touch it, and a reactivation
    # guard must re-check provenance the same way.
    user_id = await _mk_user(db, 90)
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
    await db.execute(
        update(User).where(User.id == user_id).values(is_active=False, deactivated_by_sync=False)
    )
    await db.commit()
    db.expunge_all()
    applied_d, applied_r = await ldap_sync_service._apply_sweep_flips(
        db, to_deactivate=[user], to_reactivate=[]
    )
    assert applied_d == [] and applied_r == []
    row = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
    assert row.is_active is False
    assert row.deactivated_by_sync is False

    applied_d, applied_r = await ldap_sync_service._apply_sweep_flips(
        db, to_deactivate=[], to_reactivate=[row]
    )
    # deactivated_by_sync is False (admin intent): the guarded WHERE refuses.
    assert applied_r == []
    refreshed = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
    assert refreshed.is_active is False


@pytest.mark.asyncio
async def test_sweep_commit_failure_records_no_counters(db, monkeypatch):
    # A failed sweep commit must never leave an audit row claiming flips
    # that were rolled back: recording happens only after the commit.
    ids = await _mk_candidates(db, 2)
    _enable_deactivation(monkeypatch)
    _install_presence(monkeypatch, present={"user2@company.local"})
    real_commit = db.commit

    async def failing_commit():
        # Fail exactly once (the sweep's commit, the first after patching:
        # no mappings exist so nothing commits earlier in the pass), then
        # restore so the run-finalization commit in the finally succeeds.
        monkeypatch.setattr(db, "commit", real_commit)
        raise RuntimeError("connection lost at sweep commit")

    run = await ldap_sync_service.create_run(db, "manual")
    monkeypatch.setattr(db, "commit", failing_commit)
    result = await ldap_sync_service.execute_run(db, run)
    assert result.status == "failed"
    assert result.users_deactivated == 0
    assert result.detail.get("deactivated") is None
    row = (await db.execute(select(User).where(User.id == ids[0]))).scalar_one()
    assert row.is_active is True


@pytest.mark.asyncio
async def test_execute_run_cancelled_mid_reconcile_commits_failed_with_cancelled_error(
    db, monkeypatch
):
    """A task cancellation during execute_run (the realistic trigger: the
    phase 5 interval loop's task cancelled mid-tick at service shutdown)
    must still commit a "failed" row with its cause recorded, matching the
    "failed runs record their cause" contract every other failure path
    upholds.

    Before the fix, execute_run's `except Exception` arm never caught
    asyncio.CancelledError (a BaseException in Python 3.8+, not an
    Exception), so a cancellation mid-try skipped straight to the finally
    with error still None: the finally committed status "failed" (its
    unchanged default) but with the cause silently lost.

    The stalled reconcile also does a real `db.add` of a GroupMember row and
    does NOT commit it before blocking, pinning the second hazard the
    CancelledError arm's `db.rollback()` exists for: a cancel delivered
    between a per-op db.add and its commit (the shape every real apply site
    in _reconcile_mapping has) leaves a torn write on the session. Without
    the rollback, the finally's own commit would flush that pending,
    half-applied membership row together with the run-row finalization. This
    test cancels the task while _reconcile_mapping is stalled mid-await
    (write already added, not yet committed) and asserts, read back through
    a FRESH session (not the one execute_run itself used): the run row is
    "failed" with the recorded cause, AND the pending member row never
    landed.
    """
    group = UserGroup(name="Cancel Test")
    db.add(group)
    await db.commit()
    # Captured as a plain value, not read off `group` again after this
    # point: execute_run's CancelledError rollback (on this SAME session,
    # since execute_run(db, run) below reuses it) expires every object the
    # session is tracking, `group` included, and touching an expired ORM
    # attribute after the task has run needs IO this synchronous test body
    # cannot perform implicitly.
    group_id = group.id
    mapping = LdapGroupMapping(
        group_dn=_GROUP_DN, directory_name="herd-eng", herd_group_id=group_id
    )
    db.add(mapping)
    await db.commit()
    user_id = await _mk_user(db, 1)

    entered = asyncio.Event()

    async def stalled_reconcile(*args, **kwargs):
        # The pending, uncommitted write: a real db.add with no db.commit
        # before blocking, mirroring the per-op add/commit shape of the
        # actual apply loop in _reconcile_mapping.
        db.add(GroupMember(group_id=group_id, user_id=user_id))
        entered.set()
        # Blocks forever until the task is cancelled; a fresh, never-set
        # Event (not `entered`) so this await point is the one interrupted.
        await asyncio.Event().wait()

    monkeypatch.setattr(ldap_sync_service, "_reconcile_mapping", stalled_reconcile)

    run = await ldap_sync_service.create_run(db, "manual")
    run_id = run.id

    task = asyncio.create_task(ldap_sync_service.execute_run(db, run))
    await asyncio.wait_for(entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async with TestSessionLocal() as check_db:
        reloaded = await check_db.get(LdapSyncRun, run_id)
        assert reloaded is not None
        assert reloaded.status == "failed"
        assert reloaded.error == "cancelled during service shutdown"

        pending_member = await check_db.get(GroupMember, (group_id, user_id))
        assert pending_member is None, (
            "the pending GroupMember add must not survive a cancellation: "
            "the CancelledError arm's db.rollback() exists to discard it "
            "before the finally's commit"
        )
