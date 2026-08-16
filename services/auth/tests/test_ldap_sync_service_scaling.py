"""Unit tests for issue #513's reconciler-scaling items (1, 2, 4) plus the
expired-instance regression fix adversarial review caught in item 1.

These pin call-count/batching behavior, not the fail-closed taxonomy
already pinned in test_ldap_sync_service.py (that suite's fakes were
retargeted from group_service.add_member to _add_member_resolved and given
a run_holder kwarg, tracking ldap_sync_service's actual call sites; no
assertion in that suite changed). Each test wraps the real service function
with a counting/recording shim via monkeypatch, rather than replacing it, so
the underlying behavior still executes for real against the in-memory
sqlite database.

The two test_regression_* cases at the bottom pin the fix for a real bug
the first #513 cut shipped: known_users held live ORM User instances, and a
LATER member's rollback (a provisioning IntegrityError, or a drift-repair
collision) expired every OTHER member's instance still sitting in that same
dict, so the NEXT member's attribute read raised MissingGreenlet OUTSIDE
any per-op try, failing the WHOLE run. Verified fail-then-pass against a
monkeypatched reproduction of the old ORM-returning get_users_by_emails
before this fix landed (not committed; the mechanism is documented here for
provenance).
"""

import uuid

import pytest
from app.database import Base
from app.models.group import GroupMember, UserGroup
from app.models.ldap_group_mapping import LdapGroupMapping
from app.models.user import User
from app.services import auth_service, group_service, ldap_service, ldap_sync_service
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

engine = create_async_engine(TEST_DATABASE_URL, echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

_PEOPLE = "ou=people,dc=company,dc=local"
_GROUP_DN_A = "cn=herd-eng,ou=groups,dc=company,dc=local"
_GROUP_DN_B = "cn=herd-qa,ou=groups,dc=company,dc=local"


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


def _entry(member_dns, dn=_GROUP_DN_A, name="herd-eng") -> ldap_service.LdapGroupEntry:
    return ldap_service.LdapGroupEntry(dn=dn, name=name, member_dns=tuple(member_dns))


def _install_directory(monkeypatch, groups: dict, resolutions: dict) -> None:
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


async def _mk_mapping(db, herd_group_id, group_dn=_GROUP_DN_A, name="herd-eng") -> uuid.UUID:
    mapping = LdapGroupMapping(group_dn=group_dn, directory_name=name, herd_group_id=herd_group_id)
    db.add(mapping)
    await db.commit()
    return mapping.id


async def _mk_ldap_user(db, n: int):
    from app.models.user import User

    user = User(email=f"user{n}@company.local", username=f"user{n}", auth_source="ldap")
    db.add(user)
    await db.commit()
    return user.id


def _wrap_call(monkeypatch, module, name: str, transform=lambda value: value):
    """Wrap module.name (an async (db, value) -> ... function) with a call
    recorder: appends transform(value) on every call, then delegates to the
    real implementation. Shared by every recording wrapper below (issue
    #513 item 10): get_users_by_emails, get_user_by_email, and
    get_group_by_name all share this exact (db, value) shape.
    """
    real = getattr(module, name)
    calls: list = []

    async def recording(db, value):
        calls.append(transform(value))
        return await real(db, value)

    monkeypatch.setattr(module, name, recording)
    return calls


# ---------------------------------------------------------------------------
# Item 1: one WHERE email IN (...) per group instead of one SELECT per member.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batched_lookup_one_call_for_a_three_member_group(db, monkeypatch):
    group_id = await _mk_group(db)
    await _mk_mapping(db, group_id)
    for n in (1, 2, 3):
        await _mk_ldap_user(db, n)

    batch_calls = _wrap_call(monkeypatch, auth_service, "get_users_by_emails", frozenset)
    single_calls = _wrap_call(monkeypatch, auth_service, "get_user_by_email")
    _install_directory(
        monkeypatch,
        {_GROUP_DN_A: _entry([_dn(1), _dn(2), _dn(3)])},
        {_dn(1): _resolved(1), _dn(2): _resolved(2), _dn(3): _resolved(3)},
    )

    run = await ldap_sync_service.run_sync(db)

    assert run.status == "success"
    assert run.members_added == 3
    # One batched call for the whole group (was 3 single-email SELECTs, one
    # per member, before item 1).
    assert len(batch_calls) == 1
    assert batch_calls[0] == {f"user{n}@company.local" for n in (1, 2, 3)}
    # No per-member fallback SELECT: every member was a batch hit.
    assert single_calls == []


@pytest.mark.asyncio
async def test_batched_lookup_empty_group_skips_the_query_entirely(db, monkeypatch):
    group_id = await _mk_group(db)
    await _mk_mapping(db, group_id)
    batch_calls = _wrap_call(monkeypatch, auth_service, "get_users_by_emails", frozenset)
    _install_directory(monkeypatch, {_GROUP_DN_A: _entry([])}, {})

    run = await ldap_sync_service.run_sync(db)

    assert run.status == "success"
    # The reconciler still calls the batch helper once (with an empty
    # email set), but get_users_by_emails itself short-circuits an empty
    # collection without issuing any SQL, so this costs nothing extra.
    assert batch_calls == [frozenset()]


@pytest.mark.asyncio
async def test_batched_lookup_provisioning_miss_still_uses_single_email_path(db, monkeypatch):
    # Item 1 explicitly keeps the per-member path for a provisioning MISS
    # (the user does not exist yet): the batch fetch happens before
    # provisioning and cannot know what this pass is about to create.
    group_id = await _mk_group(db)
    await _mk_mapping(db, group_id)
    batch_calls = _wrap_call(monkeypatch, auth_service, "get_users_by_emails", frozenset)
    _install_directory(monkeypatch, {_GROUP_DN_A: _entry([_dn(1)])}, {_dn(1): _resolved(1)})

    run = await ldap_sync_service.run_sync(db)

    assert run.status == "success"
    assert run.users_provisioned == 1
    assert len(batch_calls) == 1
    # The batch call still fired (proving the miss), but resolved nothing.
    assert batch_calls[0] == {"user1@company.local"}


# ---------------------------------------------------------------------------
# Item 2: run-level memo, a user in N mapped groups resolves once.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shared_member_across_two_groups_resolved_once(db, monkeypatch):
    group_a = await _mk_group(db, name="Engineering")
    group_b = await _mk_group(db, name="QA")
    await _mk_mapping(db, group_a, group_dn=_GROUP_DN_A, name="herd-eng")
    await _mk_mapping(db, group_b, group_dn=_GROUP_DN_B, name="herd-qa")
    await _mk_ldap_user(db, 1)  # the shared member
    await _mk_ldap_user(db, 2)  # herd-eng only
    await _mk_ldap_user(db, 3)  # herd-qa only

    batch_calls = _wrap_call(monkeypatch, auth_service, "get_users_by_emails", frozenset)
    single_calls = _wrap_call(monkeypatch, auth_service, "get_user_by_email")
    _install_directory(
        monkeypatch,
        {
            _GROUP_DN_A: _entry([_dn(1), _dn(2)], dn=_GROUP_DN_A, name="herd-eng"),
            _GROUP_DN_B: _entry([_dn(1), _dn(3)], dn=_GROUP_DN_B, name="herd-qa"),
        },
        {
            _dn(1): _resolved(1),
            _dn(2): _resolved(2),
            _dn(3): _resolved(3),
        },
    )

    run = await ldap_sync_service.run_sync(db)

    assert run.status == "success"
    assert run.members_added == 4  # user1 into both groups, user2, user3
    assert single_calls == []
    # Two batch calls (one per group, item 1). Mapping processing order is
    # not pinned (LdapGroupMapping.created_at can tie at sqlite's
    # resolution, falling back to a random UUID), so assert the
    # order-independent shape: user1's email appears in exactly the FIRST
    # call actually made (whichever group that was), never the second,
    # because item 2's run-level memo already resolved it by then.
    assert len(batch_calls) == 2
    assert "user1@company.local" in batch_calls[0]
    assert "user1@company.local" not in batch_calls[1]
    # The second call's set is exactly its group's OTHER member (one email,
    # since user1 was excluded by the memo).
    assert len(batch_calls[1]) == 1
    assert batch_calls[0] | batch_calls[1] == {
        "user1@company.local",
        "user2@company.local",
        "user3@company.local",
    }


@pytest.mark.asyncio
async def test_shared_member_skip_reason_replayed_per_group(db, monkeypatch):
    # A user resolved as inactive in group A's pass must still be recorded
    # as a skip against group B's own group_dn/member_dn on the memo hit,
    # not silently dropped from group B's detail.
    group_a = await _mk_group(db, name="Engineering")
    group_b = await _mk_group(db, name="QA")
    await _mk_mapping(db, group_a, group_dn=_GROUP_DN_A, name="herd-eng")
    await _mk_mapping(db, group_b, group_dn=_GROUP_DN_B, name="herd-qa")
    user_id = await _mk_ldap_user(db, 1)
    from app.models.user import User

    user = await db.get(User, user_id)
    user.is_active = False
    await db.commit()

    _install_directory(
        monkeypatch,
        {
            _GROUP_DN_A: _entry([_dn(1)], dn=_GROUP_DN_A, name="herd-eng"),
            _GROUP_DN_B: _entry([_dn(1)], dn=_GROUP_DN_B, name="herd-qa"),
        },
        {_dn(1): _resolved(1)},
    )

    run = await ldap_sync_service.run_sync(db)

    assert run.status == "partial"
    assert run.members_added == 0
    skipped = run.detail["skipped_members"]
    assert len(skipped) == 2
    assert {r["group_dn"] for r in skipped} == {_GROUP_DN_A, _GROUP_DN_B}
    assert all(r["reason"] == ldap_sync_service.MEMBER_SKIP_USER_INACTIVE for r in skipped)
    # members_skipped counts BOTH occurrences: a memo hit still counts as a
    # skip for the group it was replayed against.
    assert run.members_skipped == 2


# ---------------------------------------------------------------------------
# Item 4: "Not Grouped" resolved once per run, not per add_member call.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_not_grouped_resolved_once_across_multiple_adds(db, monkeypatch):
    group_a = await _mk_group(db, name="Engineering")
    group_b = await _mk_group(db, name="QA")
    await _mk_mapping(db, group_a, group_dn=_GROUP_DN_A, name="herd-eng")
    await _mk_mapping(db, group_b, group_dn=_GROUP_DN_B, name="herd-qa")
    not_grouped = UserGroup(name="Not Grouped")
    db.add(not_grouped)
    await db.commit()
    u1 = await _mk_ldap_user(db, 1)
    u2 = await _mk_ldap_user(db, 2)
    u3 = await _mk_ldap_user(db, 3)
    for uid in (u1, u2, u3):
        db.add(GroupMember(group_id=not_grouped.id, user_id=uid))
    await db.commit()

    name_calls = _wrap_call(monkeypatch, group_service, "get_group_by_name")
    _install_directory(
        monkeypatch,
        {
            _GROUP_DN_A: _entry([_dn(1), _dn(2)], dn=_GROUP_DN_A, name="herd-eng"),
            _GROUP_DN_B: _entry([_dn(3)], dn=_GROUP_DN_B, name="herd-qa"),
        },
        {_dn(1): _resolved(1), _dn(2): _resolved(2), _dn(3): _resolved(3)},
    )

    run = await ldap_sync_service.run_sync(db)

    assert run.status == "success"
    assert run.members_added == 3
    # Three adds across two groups; the pre-#513 code called
    # get_group_by_name("Not Grouped") TWICE per add (once in add_member,
    # once inside _remove_from_not_grouped) = 6 calls. Item 4 resolves it
    # ONCE for the whole run.
    not_grouped_calls = [c for c in name_calls if c == "Not Grouped"]
    assert len(not_grouped_calls) == 1
    # And the auto-remove actually happened for all three.
    remaining = (
        await db.execute(
            GroupMember.__table__.select().where(GroupMember.group_id == not_grouped.id)
        )
    ).all()
    assert remaining == []


@pytest.mark.asyncio
async def test_not_grouped_resolved_once_across_add_and_provision_paths(db, monkeypatch):
    # Round-3 item 4: mixes BOTH consumers of the shared NotGroupedResolver
    # in one run: group A adds an EXISTING member (the explicit
    # _add_member_resolved path in _reconcile_mapping's add loop), group B
    # provisions a brand-NEW member (create_ldap_user's own internal
    # auto-assign path). Both must resolve "Not Grouped" from the SAME
    # object, exactly once for the whole run.
    from datetime import datetime, timedelta, timezone

    group_a = await _mk_group(db, name="Engineering")
    group_b = await _mk_group(db, name="QA")
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    await _mk_mapping_ordered(db, group_a, _GROUP_DN_A, "herd-eng", base)
    await _mk_mapping_ordered(db, group_b, _GROUP_DN_B, "herd-qa", base + timedelta(seconds=1))
    not_grouped = UserGroup(name="Not Grouped")
    db.add(not_grouped)
    await db.commit()

    existing_id = await _mk_ldap_user(db, 1)
    db.add(GroupMember(group_id=not_grouped.id, user_id=existing_id))
    await db.commit()

    name_calls = _wrap_call(monkeypatch, group_service, "get_group_by_name")
    _install_directory(
        monkeypatch,
        {
            _GROUP_DN_A: _entry([_dn(1)], dn=_GROUP_DN_A, name="herd-eng"),
            _GROUP_DN_B: _entry([_dn(2)], dn=_GROUP_DN_B, name="herd-qa"),
        },
        {_dn(1): _resolved(1), _dn(2): _resolved(2)},
    )

    run = await ldap_sync_service.run_sync(db)

    assert run.status == "success"
    assert run.members_added == 2
    assert run.users_provisioned == 1
    not_grouped_calls = [c for c in name_calls if c == "Not Grouped"]
    assert len(not_grouped_calls) == 1

    # Both paths actually landed: the existing member added via
    # _add_member_resolved, the new one via create_ldap_user's provisioning.
    member_ids_a = (
        (await db.execute(select(GroupMember.user_id).where(GroupMember.group_id == group_a)))
        .scalars()
        .all()
    )
    assert existing_id in member_ids_a
    provisioned = await auth_service.get_user_by_email(db, "user2@company.local")
    assert provisioned is not None
    member_ids_b = (
        (await db.execute(select(GroupMember.user_id).where(GroupMember.group_id == group_b)))
        .scalars()
        .all()
    )
    assert provisioned.id in member_ids_b
    # Both were auto-removed from "Not Grouped": the existing member
    # (pre-seeded there) and the newly provisioned one (auto-assigned
    # there by create_ldap_user, then immediately auto-removed on its own
    # add to group_b within the SAME reconcile pass).
    remaining_not_grouped = (
        (
            await db.execute(
                select(GroupMember.user_id).where(GroupMember.group_id == not_grouped.id)
            )
        )
        .scalars()
        .all()
    )
    assert remaining_not_grouped == []


@pytest.mark.asyncio
async def test_not_grouped_missing_is_cached_as_none_no_op(db, monkeypatch):
    # No "Not Grouped" group exists at all: add_member must still succeed,
    # and the resolved-missing state (None) must not trigger a repeated
    # by-name lookup or a query in _remove_from_not_grouped.
    group_id = await _mk_group(db)
    await _mk_mapping(db, group_id)
    await _mk_ldap_user(db, 1)
    name_calls = _wrap_call(monkeypatch, group_service, "get_group_by_name")
    _install_directory(monkeypatch, {_GROUP_DN_A: _entry([_dn(1)])}, {_dn(1): _resolved(1)})

    run = await ldap_sync_service.run_sync(db)

    assert run.status == "success"
    assert run.members_added == 1
    assert len([c for c in name_calls if c == "Not Grouped"]) == 1


# ---------------------------------------------------------------------------
# Item 1 regression: known_users must never hold ORM instances (issue #513
# adversarial review). Both scenarios put a member whose processing
# triggers a mid-loop rollback BEFORE a preloaded, no-mutation-needed
# member in directory order, so a stale ORM instance in known_users would
# blow up on the second member's classification.
# ---------------------------------------------------------------------------


async def _mk_named_user(db, *, email, username, auth_source="ldap") -> uuid.UUID:
    user = User(
        email=email,
        username=username,
        hashed_password=None if auth_source == "ldap" else "fake",
        auth_source=auth_source,
    )
    db.add(user)
    await db.commit()
    return user.id


@pytest.mark.asyncio
async def test_regression_username_taken_member_then_preloaded_member(db, monkeypatch):
    # Member 1 (dn(1)): a NEW identity whose desired username collides with
    # an existing (different-email) account, triggering create_ldap_user's
    # IntegrityError-rollback path (MEMBER_SKIP_USERNAME_TAKEN).
    # Member 2 (dn(2)): an EXISTING ldap user needing no mutation, present
    # in the SAME group's known_users batch, processed immediately after.
    group_id = await _mk_group(db)
    await _mk_mapping(db, group_id)
    await _mk_named_user(db, email="blocker@company.local", username="shared", auth_source="local")
    member2_id = await _mk_named_user(db, email="user2@company.local", username="user2")

    identity1 = ldap_service.LdapIdentity(username="shared", email="user1@company.local", dn=_dn(1))
    identity2 = ldap_service.LdapIdentity(username="user2", email="user2@company.local", dn=_dn(2))
    _install_directory(
        monkeypatch,
        {_GROUP_DN_A: _entry([_dn(1), _dn(2)])},
        {
            _dn(1): ldap_service.LdapMemberResolution(identity1),
            _dn(2): ldap_service.LdapMemberResolution(identity2),
        },
    )

    run = await ldap_sync_service.run_sync(db)

    # Member 1 skipped (username collision degrades the run to "partial",
    # NOT "failed"); member 2 still added despite running right after the
    # rollback that would have expired a stale ORM instance.
    assert run.status == "partial"
    assert run.members_added == 1
    assert run.detail["skipped_members"] == [
        {
            "group_dn": _GROUP_DN_A,
            "member_dn": _dn(1),
            "reason": ldap_sync_service.MEMBER_SKIP_USERNAME_TAKEN,
        }
    ]
    member_ids = (await db.execute(select(GroupMember.user_id))).scalars().all()
    assert member2_id in member_ids


@pytest.mark.asyncio
async def test_regression_drift_collision_member_then_preloaded_member(db, monkeypatch):
    # Member 1 (dn(1)): an EXISTING ldap user whose directory username
    # collides with a DIFFERENT existing account, triggering the drift
    # repair's own IntegrityError-rollback path (a drift collision; the
    # member still reconciles, but the repair itself rolled back).
    # Member 2 (dn(2)): another EXISTING, no-mutation-needed member in the
    # same batch, processed immediately after.
    group_id = await _mk_group(db)
    await _mk_mapping(db, group_id)
    await _mk_named_user(db, email="blocker2@company.local", username="taken2")
    member1_id = await _mk_named_user(db, email="user1@company.local", username="old_name")
    member2_id = await _mk_named_user(db, email="user2@company.local", username="user2")

    identity1 = ldap_service.LdapIdentity(username="taken2", email="user1@company.local", dn=_dn(1))
    identity2 = ldap_service.LdapIdentity(username="user2", email="user2@company.local", dn=_dn(2))
    _install_directory(
        monkeypatch,
        {_GROUP_DN_A: _entry([_dn(1), _dn(2)])},
        {
            _dn(1): ldap_service.LdapMemberResolution(identity1),
            _dn(2): ldap_service.LdapMemberResolution(identity2),
        },
    )

    run = await ldap_sync_service.run_sync(db)

    # The drift collision degrades the run to "partial" but does NOT skip
    # the member: both members reconcile into the group.
    assert run.status == "partial"
    assert run.members_added == 2
    assert run.detail["drift_collisions"] == [
        {
            "group_dn": _GROUP_DN_A,
            "member_dn": _dn(1),
            "reason": ldap_sync_service.MEMBER_SKIP_USERNAME_DRIFT_COLLISION,
        }
    ]
    member_ids = (await db.execute(select(GroupMember.user_id))).scalars().all()
    assert member1_id in member_ids
    assert member2_id in member_ids


# ---------------------------------------------------------------------------
# Item 5: MEMBER_SKIP_USERNAME_TAKEN is NOT memoized across groups, since a
# later member's drift repair can free the blocked username within the SAME
# run.
# ---------------------------------------------------------------------------


async def _mk_mapping_ordered(db, herd_group_id, group_dn, name, created_at):

    mapping = LdapGroupMapping(
        group_dn=group_dn,
        directory_name=name,
        herd_group_id=herd_group_id,
        created_at=created_at,
    )
    db.add(mapping)
    await db.commit()
    return mapping.id


@pytest.mark.asyncio
async def test_username_taken_skip_not_memoized_across_groups(db, monkeypatch):
    from datetime import datetime, timedelta, timezone

    _GROUP_DN_C = "cn=herd-ops,ou=groups,dc=company,dc=local"
    group_a = await _mk_group(db, name="Engineering")
    group_b = await _mk_group(db, name="QA")
    group_c = await _mk_group(db, name="Ops")
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # Explicit, strictly increasing created_at: execute_run orders mappings
    # by (created_at, id), and sqlite's func.now() resolution is too coarse
    # to guarantee A-before-B-before-C from wall-clock inserts alone.
    await _mk_mapping_ordered(db, group_a, _GROUP_DN_A, "herd-eng", base)
    await _mk_mapping_ordered(db, group_b, _GROUP_DN_B, "herd-qa", base + timedelta(seconds=1))
    await _mk_mapping_ordered(db, group_c, _GROUP_DN_C, "herd-ops", base + timedelta(seconds=2))

    # blocker holds "shared_name"; target (not yet provisioned) wants it too.
    blocker_id = await _mk_named_user(db, email="blocker@company.local", username="shared_name")

    identity_target = ldap_service.LdapIdentity(
        username="shared_name", email="target@company.local", dn=_dn(1)
    )
    # Group B's directory entry for blocker uses a DIFFERENT username,
    # triggering a (collision-free) drift repair that frees "shared_name".
    identity_blocker_renamed = ldap_service.LdapIdentity(
        username="renamed", email="blocker@company.local", dn=_dn(2)
    )

    _install_directory(
        monkeypatch,
        {
            _GROUP_DN_A: _entry([_dn(1)], dn=_GROUP_DN_A, name="herd-eng"),
            _GROUP_DN_B: _entry([_dn(2)], dn=_GROUP_DN_B, name="herd-qa"),
            _GROUP_DN_C: _entry([_dn(1)], dn=_GROUP_DN_C, name="herd-ops"),
        },
        {
            _dn(1): ldap_service.LdapMemberResolution(identity_target),
            _dn(2): ldap_service.LdapMemberResolution(identity_blocker_renamed),
        },
    )

    run = await ldap_sync_service.run_sync(db)

    # Group A's occurrence of target@company.local is skipped
    # (USERNAME_TAKEN, "shared_name" still held by blocker at that point).
    skipped = run.detail.get("skipped_members", [])
    assert any(
        r["group_dn"] == _GROUP_DN_A and r["reason"] == ldap_sync_service.MEMBER_SKIP_USERNAME_TAKEN
        for r in skipped
    )
    # Group B renames blocker away, freeing "shared_name".
    blocker = await db.get(User, blocker_id)
    assert blocker.username == "renamed"
    # Group C's LATER occurrence of the SAME email must NOT replay a cached
    # skip: since it was never memoized, it re-attempts from scratch and
    # NOW succeeds, because "shared_name" is free.
    target = await auth_service.get_user_by_email(db, "target@company.local")
    assert target is not None
    assert target.username == "shared_name"
    member_ids_c = (
        (await db.execute(select(GroupMember.user_id).where(GroupMember.group_id == group_c)))
        .scalars()
        .all()
    )
    assert target.id in member_ids_c
    # Group A never got the member (it was skipped there, not retro-added).
    member_ids_a = (
        (await db.execute(select(GroupMember.user_id).where(GroupMember.group_id == group_a)))
        .scalars()
        .all()
    )
    assert target.id not in member_ids_a


# ---------------------------------------------------------------------------
# Round-3 item 1: is_active TOCTOU widening. identity_cache no longer
# memoizes MEMBER_SKIP_USER_INACTIVE, and the add loop re-verifies is_active
# live right before an actual add, so a mid-run reactivation/deactivation
# never produces a spurious removal or a spurious add.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reactivate_mid_run_does_not_remove(db, monkeypatch):
    from datetime import datetime, timedelta, timezone

    group_a = await _mk_group(db, name="Engineering")
    group_b = await _mk_group(db, name="QA")
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    await _mk_mapping_ordered(db, group_a, _GROUP_DN_A, "herd-eng", base)
    await _mk_mapping_ordered(db, group_b, _GROUP_DN_B, "herd-qa", base + timedelta(seconds=1))

    # X starts INACTIVE, already has an existing membership row in group B
    # (from a prior run), and the directory lists X as a member of BOTH
    # groups in THIS run.
    x_id = await _mk_named_user(db, email="x@company.local", username="x")
    x = await db.get(User, x_id)
    x.is_active = False
    await db.commit()
    db.add(GroupMember(group_id=group_b, user_id=x_id))
    await db.commit()

    identity_x = ldap_service.LdapIdentity(username="x", email="x@company.local", dn=_dn(1))

    async def fake_fetch_group(group_dn, *, run_holder=None):
        return _entry(
            [_dn(1)], dn=group_dn, name="herd-eng" if group_dn == _GROUP_DN_A else "herd-qa"
        )

    async def fake_resolve_members(member_dns, *, run_holder=None):
        if group_dn_context["current"] == _GROUP_DN_B:
            # Reactivated by an admin BETWEEN group A's (cached-nothing,
            # since inactive is no longer cacheable) classification and
            # group B's own independent one.
            fresh = await db.get(User, x_id)
            fresh.is_active = True
            await db.commit()
        return [ldap_service.LdapMemberResolution(identity_x) for _ in member_dns]

    # Track which group's fetch_group call is in flight so
    # fake_resolve_members knows whether to reactivate; fetch_group runs
    # immediately before resolve_members for each mapping.
    group_dn_context = {"current": None}

    async def fetch_group_tracking(group_dn, *, run_holder=None):
        group_dn_context["current"] = group_dn
        return await fake_fetch_group(group_dn, run_holder=run_holder)

    monkeypatch.setattr(ldap_service, "fetch_group", fetch_group_tracking)
    monkeypatch.setattr(ldap_service, "resolve_members", fake_resolve_members)

    run = await ldap_sync_service.run_sync(db)

    # X must still be a member of group B: reactivated before group B's
    # own fresh (uncached) classification, so group B correctly resolves X
    # as an active, proven directory member, matching current_ids (which
    # also now sees X as active).
    member_ids_b = (
        (await db.execute(select(GroupMember.user_id).where(GroupMember.group_id == group_b)))
        .scalars()
        .all()
    )
    assert x_id in member_ids_b
    assert run.members_removed == 0


@pytest.mark.asyncio
async def test_deactivate_mid_run_does_not_add(db, monkeypatch):
    from datetime import datetime, timedelta, timezone

    group_a = await _mk_group(db, name="Engineering")
    group_b = await _mk_group(db, name="QA")
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    await _mk_mapping_ordered(db, group_a, _GROUP_DN_A, "herd-eng", base)
    await _mk_mapping_ordered(db, group_b, _GROUP_DN_B, "herd-qa", base + timedelta(seconds=1))

    # Y starts ACTIVE, has NO existing membership in either group, and the
    # directory lists Y as a member of BOTH groups in THIS run.
    y_id = await _mk_named_user(db, email="y@company.local", username="y")
    identity_y = ldap_service.LdapIdentity(username="y", email="y@company.local", dn=_dn(2))

    group_dn_context = {"current": None}

    async def fetch_group_tracking(group_dn, *, run_holder=None):
        group_dn_context["current"] = group_dn
        return _entry(
            [_dn(2)], dn=group_dn, name="herd-eng" if group_dn == _GROUP_DN_A else "herd-qa"
        )

    async def fake_resolve_members(member_dns, *, run_holder=None):
        if group_dn_context["current"] == _GROUP_DN_B:
            # Deactivated by an admin AFTER group A cached Y's success but
            # BEFORE group B's add actually runs.
            fresh = await db.get(User, y_id)
            fresh.is_active = False
            await db.commit()
        return [ldap_service.LdapMemberResolution(identity_y) for _ in member_dns]

    monkeypatch.setattr(ldap_service, "fetch_group", fetch_group_tracking)
    monkeypatch.setattr(ldap_service, "resolve_members", fake_resolve_members)

    run = await ldap_sync_service.run_sync(db)

    # Group A added Y (active at the time). Group B's cache hit on Y's
    # SUCCESS would naively add Y too, but the add-loop's live
    # re-verification must catch that Y is now inactive and skip it.
    member_ids_a = (
        (await db.execute(select(GroupMember.user_id).where(GroupMember.group_id == group_a)))
        .scalars()
        .all()
    )
    member_ids_b = (
        (await db.execute(select(GroupMember.user_id).where(GroupMember.group_id == group_b)))
        .scalars()
        .all()
    )
    assert y_id in member_ids_a
    assert y_id not in member_ids_b
    assert run.status == "partial"
    skipped = run.detail.get("skipped_members", [])
    assert any(
        r["group_dn"] == _GROUP_DN_B and r["reason"] == ldap_sync_service.MEMBER_SKIP_USER_INACTIVE
        for r in skipped
    )


def test_cache_outcome_only_caches_the_structural_frozenset():
    identity_cache: dict = {}
    user_id = uuid.uuid4()

    # Success is always cached, regardless of the frozenset.
    ldap_sync_service._cache_outcome(identity_cache, "success@company.local", user_id)
    assert identity_cache["success@company.local"].user_id == user_id
    assert identity_cache["success@company.local"].skip_reason is None

    # A reason IN _CACHEABLE_SKIP_REASONS is cached.
    cacheable_reason = next(iter(ldap_sync_service._CACHEABLE_SKIP_REASONS))
    ldap_sync_service._cache_outcome(
        identity_cache, "cacheable@company.local", None, cacheable_reason
    )
    assert identity_cache["cacheable@company.local"].skip_reason == cacheable_reason

    # MEMBER_SKIP_USER_INACTIVE is explicitly excluded from the frozenset.
    assert (
        ldap_sync_service.MEMBER_SKIP_USER_INACTIVE not in ldap_sync_service._CACHEABLE_SKIP_REASONS
    )
    ldap_sync_service._cache_outcome(
        identity_cache, "inactive@company.local", None, ldap_sync_service.MEMBER_SKIP_USER_INACTIVE
    )
    assert "inactive@company.local" not in identity_cache

    # MEMBER_SKIP_USERNAME_TAKEN is likewise excluded (round-2 item 5).
    ldap_sync_service._cache_outcome(
        identity_cache, "taken@company.local", None, ldap_sync_service.MEMBER_SKIP_USERNAME_TAKEN
    )
    assert "taken@company.local" not in identity_cache

    # A brand-new, never-declared skip reason is NEVER cached by default:
    # a future skip reason must be added to _CACHEABLE_SKIP_REASONS
    # deliberately, not inherited by copy-pasting a call site.
    ldap_sync_service._cache_outcome(identity_cache, "novel@company.local", None, "some_new_reason")
    assert "novel@company.local" not in identity_cache
