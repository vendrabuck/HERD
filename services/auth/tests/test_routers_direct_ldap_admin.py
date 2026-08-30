"""Direct handler-call tests for ldap_sync.py, admin.py, and tokens.py
(issue coverage lane).

pytest-cov's sysmon core loses line-hit attribution for lines that run after
the first `await` inside a coroutine driven through httpx's ASGITransport
(see tests/test_routers_direct.py's own docstring, and
memory/reference-herd-async-coverage-artifact.md): the ASGI-path tests in
test_ldap_sync.py, test_auth.py, and test_api_tokens.py already pin the
BEHAVIOR of every branch in these three routers (exact status codes,
response bodies, and the proof-discipline rules in ldap_sync.py's module
docstring), but the tracer does not credit the post-await router-body lines
those tests exercise. This file re-drives the same branches by calling the
router functions directly with a real DB session and a mock caller, the
established workaround so coverage reflects what is actually tested.

Behavior already covered by ASGI tests is not re-asserted in detail here
beyond what proves the branch taken; do not treat this file as the primary
spec for these routers.
"""

import uuid

import pytest
from app.models.ldap_group_mapping import LdapGroupMapping
from app.models.ldap_sync_run import LdapSyncRun
from app.models.user import Role
from app.services import ldap_service, ldap_sync_service
from app.services.group_service import create_group
from fastapi import HTTPException

from tests._harness import TestSessionLocal
from tests._harness import mock_user as _mock_user

_GROUP_DN = "cn=herd-eng,ou=groups,dc=company,dc=local"


def _entry(dn=_GROUP_DN, name="herd-eng", member_dns=("uid=user1,ou=people,dc=company,dc=local",)):
    return ldap_service.LdapGroupEntry(dn=dn, name=name, member_dns=member_dns)


@pytest.fixture(autouse=True)
def ldap_mode(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "auth_method", "ldap", raising=False)


# ---------------------------------------------------------------------------
# ldap_sync router: create_mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_mapping_direct_success_caches_name(monkeypatch):
    from app.routers.ldap_sync import create_mapping
    from app.schemas.ldap_sync import MappingCreateRequest

    monkeypatch.setattr(ldap_service, "fetch_group", lambda dn: _fake_fetch(_entry(dn=dn)))
    admin = _mock_user(Role.ADMIN)
    async with TestSessionLocal() as db:
        group = await create_group(db, "Engineering", None, admin.id)
        body = MappingCreateRequest(group_dn=_GROUP_DN, herd_group_id=group.id)
        result = await create_mapping(body, db=db, current_user=admin)
        assert result.group_dn == _GROUP_DN
        assert result.directory_name == "herd-eng"
        assert result.warning is None


async def _fake_fetch(entry=None, error=None):
    if error is not None:
        raise error
    return entry


@pytest.mark.asyncio
async def test_create_mapping_direct_no_members_warns(monkeypatch):
    from app.routers.ldap_sync import NO_MEMBERS_WARNING, create_mapping
    from app.schemas.ldap_sync import MappingCreateRequest

    empty_entry = _entry(member_dns=())
    monkeypatch.setattr(ldap_service, "fetch_group", lambda dn: _fake_fetch(empty_entry))
    admin = _mock_user(Role.ADMIN)
    async with TestSessionLocal() as db:
        group = await create_group(db, "Empty", None, admin.id)
        body = MappingCreateRequest(group_dn=_GROUP_DN, herd_group_id=group.id)
        result = await create_mapping(body, db=db, current_user=admin)
        assert result.warning == NO_MEMBERS_WARNING


@pytest.mark.asyncio
async def test_create_mapping_direct_refused_outside_ldap_mode(monkeypatch):
    from app.config import settings
    from app.routers.ldap_sync import create_mapping
    from app.schemas.ldap_sync import MappingCreateRequest

    monkeypatch.setattr(settings, "auth_method", "local", raising=False)
    admin = _mock_user(Role.ADMIN)
    async with TestSessionLocal() as db:
        body = MappingCreateRequest(group_dn=_GROUP_DN, herd_group_id=uuid.uuid4())
        with pytest.raises(HTTPException) as exc:
            await create_mapping(body, db=db, current_user=admin)
        assert exc.value.status_code == 409
        assert "auth_method" in exc.value.detail


@pytest.mark.asyncio
async def test_create_mapping_direct_group_not_found():
    from app.routers.ldap_sync import create_mapping
    from app.schemas.ldap_sync import MappingCreateRequest

    admin = _mock_user(Role.ADMIN)
    async with TestSessionLocal() as db:
        body = MappingCreateRequest(group_dn=_GROUP_DN, herd_group_id=uuid.uuid4())
        with pytest.raises(HTTPException) as exc:
            await create_mapping(body, db=db, current_user=admin)
        assert exc.value.status_code == 404
        assert exc.value.detail == "HERD group not found"


@pytest.mark.asyncio
async def test_create_mapping_direct_duplicate_dn_precheck_409():
    from app.routers.ldap_sync import create_mapping
    from app.schemas.ldap_sync import MappingCreateRequest

    admin = _mock_user(Role.ADMIN)
    async with TestSessionLocal() as db:
        group_a = await create_group(db, "A", None, admin.id)
        group_b = await create_group(db, "B", None, admin.id)
        existing = LdapGroupMapping(
            group_dn=_GROUP_DN, directory_name="herd-eng", herd_group_id=group_a.id
        )
        db.add(existing)
        await db.commit()

        body = MappingCreateRequest(group_dn=_GROUP_DN, herd_group_id=group_b.id)
        with pytest.raises(HTTPException) as exc:
            await create_mapping(body, db=db, current_user=admin)
        assert exc.value.status_code == 409
        assert "already exists" in exc.value.detail


@pytest.mark.asyncio
async def test_create_mapping_direct_group_already_mapped_precheck_409():
    from app.routers.ldap_sync import create_mapping
    from app.schemas.ldap_sync import MappingCreateRequest

    admin = _mock_user(Role.ADMIN)
    async with TestSessionLocal() as db:
        group = await create_group(db, "A", None, admin.id)
        existing = LdapGroupMapping(
            group_dn=_GROUP_DN, directory_name="herd-eng", herd_group_id=group.id
        )
        db.add(existing)
        await db.commit()

        body = MappingCreateRequest(
            group_dn="cn=other,ou=groups,dc=company,dc=local", herd_group_id=group.id
        )
        with pytest.raises(HTTPException) as exc:
            await create_mapping(body, db=db, current_user=admin)
        assert exc.value.status_code == 409
        assert "already has a directory mapping" in exc.value.detail


@pytest.mark.asyncio
async def test_create_mapping_direct_directory_unavailable_503(monkeypatch):
    from app.routers.ldap_sync import create_mapping
    from app.schemas.ldap_sync import MappingCreateRequest

    err = ldap_service.LdapUnavailableError("bind failed")
    monkeypatch.setattr(ldap_service, "fetch_group", lambda dn: _fake_fetch(error=err))
    admin = _mock_user(Role.ADMIN)
    async with TestSessionLocal() as db:
        group = await create_group(db, "A", None, admin.id)
        body = MappingCreateRequest(group_dn=_GROUP_DN, herd_group_id=group.id)
        with pytest.raises(HTTPException) as exc:
            await create_mapping(body, db=db, current_user=admin)
        assert exc.value.status_code == 503
        assert "not validated" in exc.value.detail


@pytest.mark.asyncio
async def test_create_mapping_direct_dangling_dn_422(monkeypatch):
    from app.routers.ldap_sync import create_mapping
    from app.schemas.ldap_sync import MappingCreateRequest

    monkeypatch.setattr(ldap_service, "fetch_group", lambda dn: _fake_fetch(None))
    admin = _mock_user(Role.ADMIN)
    async with TestSessionLocal() as db:
        group = await create_group(db, "A", None, admin.id)
        body = MappingCreateRequest(group_dn=_GROUP_DN, herd_group_id=group.id)
        with pytest.raises(HTTPException) as exc:
            await create_mapping(body, db=db, current_user=admin)
        assert exc.value.status_code == 422
        assert "does not resolve" in exc.value.detail


@pytest.mark.asyncio
async def test_create_mapping_direct_race_group_deleted_after_lookup(monkeypatch):
    """The pre-check and the directory round-trip both find the group, but a
    concurrent delete removes it before the insert commits: the IntegrityError
    branch must re-check existence and report 404, not a generic conflict.

    db.commit() is forced to raise once so the router's real except-block
    runs; the group row is deleted (from a second, independent session) just
    before that commit to make the except-block's own re-check for real
    find the group gone.
    """
    from app.models.group import UserGroup
    from app.routers.ldap_sync import create_mapping
    from app.schemas.ldap_sync import MappingCreateRequest
    from sqlalchemy import delete
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.ext.asyncio import AsyncSession

    monkeypatch.setattr(ldap_service, "fetch_group", lambda dn: _fake_fetch(_entry()))
    admin = _mock_user(Role.ADMIN)
    async with TestSessionLocal() as db:
        group = await create_group(db, "Racy", None, admin.id)
        body = MappingCreateRequest(group_dn=_GROUP_DN, herd_group_id=group.id)

        real_commit = AsyncSession.commit
        raised = {"done": False}

        async def fake_commit(self):
            if self is db and not raised["done"]:
                raised["done"] = True
                async with TestSessionLocal() as other_db:
                    await other_db.execute(delete(UserGroup).where(UserGroup.id == group.id))
                    await other_db.commit()
                raise IntegrityError("insert", {}, Exception("fk violation"))
            await real_commit(self)

        monkeypatch.setattr(AsyncSession, "commit", fake_commit)
        with pytest.raises(HTTPException) as exc:
            await create_mapping(body, db=db, current_user=admin)
        assert exc.value.status_code == 404
        assert exc.value.detail == "HERD group not found"


@pytest.mark.asyncio
async def test_create_mapping_direct_race_concurrent_duplicate(monkeypatch):
    """A concurrent create wins the unique constraint between this request's
    pre-check and its commit: the IntegrityError branch must report the
    conflicting-mapping detail from the unique columns, not the generic
    fallback. Bypasses the pre-check with a stub so a real row inserted just
    before create_mapping runs is the thing that trips SQLite's own UNIQUE
    constraint on db.commit(), exercising the router's real except-block.
    """
    from app.routers import ldap_sync as ldap_sync_router
    from app.routers.ldap_sync import _conflicting_mapping_detail as real_check
    from app.routers.ldap_sync import create_mapping
    from app.schemas.ldap_sync import MappingCreateRequest

    monkeypatch.setattr(ldap_service, "fetch_group", lambda dn: _fake_fetch(_entry()))
    admin = _mock_user(Role.ADMIN)
    async with TestSessionLocal() as db:
        group = await create_group(db, "Racy2", None, admin.id)
        winner_group = await create_group(db, "Winner", None, admin.id)
        body = MappingCreateRequest(group_dn=_GROUP_DN, herd_group_id=group.id)

        # Only the FIRST call (the pre-check, which must pass so we reach
        # db.commit()) is bypassed; the SECOND call is the router's own
        # post-IntegrityError re-check and must run for real, since that is
        # the exact branch this test targets.
        calls = {"n": 0}

        async def bypass_precheck_only(db, group_dn, herd_group_id):
            calls["n"] += 1
            if calls["n"] == 1:
                return None
            return await real_check(db, group_dn, herd_group_id)

        monkeypatch.setattr(ldap_sync_router, "_conflicting_mapping_detail", bypass_precheck_only)

        async with TestSessionLocal() as other_db:
            other_db.add(
                LdapGroupMapping(
                    group_dn=_GROUP_DN, directory_name="herd-eng", herd_group_id=winner_group.id
                )
            )
            await other_db.commit()

        with pytest.raises(HTTPException) as exc:
            await create_mapping(body, db=db, current_user=admin)
        assert exc.value.status_code == 409
        assert exc.value.detail == "A mapping for this group_dn already exists"


@pytest.mark.asyncio
async def test_create_mapping_direct_race_no_matching_constraint_falls_back(monkeypatch):
    """An IntegrityError that the post-rollback re-check cannot attribute to
    either unique column (both stay clear) falls back to the generic retry
    message rather than misreporting a specific conflict. db.commit() is
    forced to raise once so the router's real except-block (including its
    real re-check, which finds nothing because nothing actually conflicts)
    runs unmodified."""
    from app.routers.ldap_sync import create_mapping
    from app.schemas.ldap_sync import MappingCreateRequest
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.ext.asyncio import AsyncSession

    monkeypatch.setattr(ldap_service, "fetch_group", lambda dn: _fake_fetch(_entry()))
    admin = _mock_user(Role.ADMIN)
    async with TestSessionLocal() as db:
        group = await create_group(db, "NoMatch", None, admin.id)
        body = MappingCreateRequest(group_dn=_GROUP_DN, herd_group_id=group.id)

        real_commit = AsyncSession.commit
        raised = {"done": False}

        async def fake_commit(self):
            if self is db and not raised["done"]:
                raised["done"] = True
                raise IntegrityError("insert", {}, Exception("some other constraint"))
            await real_commit(self)

        monkeypatch.setattr(AsyncSession, "commit", fake_commit)
        with pytest.raises(HTTPException) as exc:
            await create_mapping(body, db=db, current_user=admin)
        assert exc.value.status_code == 409
        assert exc.value.detail == "Mapping conflicts with concurrent changes; retry"


# ---------------------------------------------------------------------------
# ldap_sync router: delete_mapping, list_mappings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_mapping_direct_success_returns_none():
    from app.routers.ldap_sync import delete_mapping

    admin = _mock_user(Role.ADMIN)
    async with TestSessionLocal() as db:
        group = await create_group(db, "DelMe", None, admin.id)
        mapping = LdapGroupMapping(
            group_dn=_GROUP_DN, directory_name="herd-eng", herd_group_id=group.id
        )
        db.add(mapping)
        await db.commit()
        mapping_id = mapping.id

        result = await delete_mapping(mapping_id, db=db, current_user=admin)
        assert result is None

        remaining = (
            await db.execute(
                LdapGroupMapping.__table__.select().where(LdapGroupMapping.id == mapping_id)
            )
        ).first()
        assert remaining is None


@pytest.mark.asyncio
async def test_delete_mapping_direct_not_found():
    from app.routers.ldap_sync import delete_mapping

    admin = _mock_user(Role.ADMIN)
    async with TestSessionLocal() as db:
        with pytest.raises(HTTPException) as exc:
            await delete_mapping(uuid.uuid4(), db=db, current_user=admin)
        assert exc.value.status_code == 404
        assert exc.value.detail == "Mapping not found"


@pytest.mark.asyncio
async def test_list_mappings_direct_returns_paginated():
    from app.routers.ldap_sync import list_mappings

    admin = _mock_user(Role.ADMIN)
    async with TestSessionLocal() as db:
        group = await create_group(db, "Listed", None, admin.id)
        db.add(
            LdapGroupMapping(group_dn=_GROUP_DN, directory_name="herd-eng", herd_group_id=group.id)
        )
        await db.commit()

        result = await list_mappings(skip=0, limit=50, db=db, _=admin)
        assert result.total == 1
        assert result.items[0].group_dn == _GROUP_DN


# ---------------------------------------------------------------------------
# ldap_sync router: start_sync_run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_sync_run_direct_refused_outside_ldap_mode(monkeypatch):
    from app.config import settings
    from app.routers.ldap_sync import start_sync_run

    monkeypatch.setattr(settings, "auth_method", "local", raising=False)
    admin = _mock_user(Role.ADMIN)
    with pytest.raises(HTTPException) as exc:
        await start_sync_run(current_user=admin)
    assert exc.value.status_code == 409
    assert "auth_method" in exc.value.detail


@pytest.mark.asyncio
async def test_start_sync_run_direct_in_process_busy_409(monkeypatch):
    from app.routers.ldap_sync import start_sync_run

    async def fake_start(trigger):
        raise ldap_sync_service.SyncBusyError("in_process")

    monkeypatch.setattr(ldap_sync_service, "start_background_run", fake_start)
    admin = _mock_user(Role.ADMIN)
    with pytest.raises(HTTPException) as exc:
        await start_sync_run(current_user=admin)
    assert exc.value.status_code == 409
    assert exc.value.detail == "A sync run is already in progress"


@pytest.mark.asyncio
async def test_start_sync_run_direct_replica_busy_409(monkeypatch):
    from app.routers.ldap_sync import start_sync_run

    async def fake_start(trigger):
        raise ldap_sync_service.SyncBusyError("replica")

    monkeypatch.setattr(ldap_sync_service, "start_background_run", fake_start)
    admin = _mock_user(Role.ADMIN)
    with pytest.raises(HTTPException) as exc:
        await start_sync_run(current_user=admin)
    assert exc.value.status_code == 409
    assert exc.value.detail == "A sync run is already in progress on another replica"


@pytest.mark.asyncio
async def test_start_sync_run_direct_accepted(monkeypatch):
    from app.routers.ldap_sync import start_sync_run

    run_id = uuid.uuid4()

    async def fake_start(trigger):
        assert trigger == "manual"
        return run_id

    monkeypatch.setattr(ldap_sync_service, "start_background_run", fake_start)
    admin = _mock_user(Role.ADMIN)
    result = await start_sync_run(current_user=admin)
    assert result.run_id == run_id


# ---------------------------------------------------------------------------
# ldap_sync router: list_sync_runs, get_sync_run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_sync_runs_direct_returns_paginated():
    from app.routers.ldap_sync import list_sync_runs

    admin = _mock_user(Role.ADMIN)
    async with TestSessionLocal() as db:
        run = LdapSyncRun(id=uuid.uuid4(), trigger="manual", status="success")
        db.add(run)
        await db.commit()

        result = await list_sync_runs(skip=0, limit=50, db=db, _=admin)
        assert result.total == 1
        assert result.items[0].id == run.id


@pytest.mark.asyncio
async def test_get_sync_run_direct_found():
    from app.routers.ldap_sync import get_sync_run

    admin = _mock_user(Role.ADMIN)
    async with TestSessionLocal() as db:
        run = LdapSyncRun(id=uuid.uuid4(), trigger="manual", status="success")
        db.add(run)
        await db.commit()

        result = await get_sync_run(run.id, db=db, _=admin)
        assert result.id == run.id
        assert result.status == "success"


@pytest.mark.asyncio
async def test_get_sync_run_direct_not_found():
    from app.routers.ldap_sync import get_sync_run

    admin = _mock_user(Role.ADMIN)
    async with TestSessionLocal() as db:
        with pytest.raises(HTTPException) as exc:
            await get_sync_run(uuid.uuid4(), db=db, _=admin)
        assert exc.value.status_code == 404
        assert exc.value.detail == "Sync run not found"


# ---------------------------------------------------------------------------
# admin router: activate_user, deactivate_user (post-await bodies)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_activate_user_direct_success_clears_sync_provenance():
    from app.routers.admin import activate_user
    from app.services.auth_service import create_user

    admin = _mock_user(Role.ADMIN)
    async with TestSessionLocal() as db:
        target = await create_user(db, "act@test.com", "act", "password123")
        target.is_active = False
        target.deactivated_by_sync = True
        await db.commit()

        result = await activate_user(target.id, db=db, current_user=admin)
        assert result.is_active is True

        await db.refresh(target)
        assert target.deactivated_by_sync is False


@pytest.mark.asyncio
async def test_activate_user_direct_not_found():
    from app.routers.admin import activate_user

    admin = _mock_user(Role.ADMIN)
    async with TestSessionLocal() as db:
        with pytest.raises(HTTPException) as exc:
            await activate_user(uuid.uuid4(), db=db, current_user=admin)
        assert exc.value.status_code == 404
        assert exc.value.detail == "User not found"


@pytest.mark.asyncio
async def test_deactivate_user_direct_success_clears_sync_provenance():
    from app.routers.admin import deactivate_user
    from app.services.auth_service import create_user

    admin = _mock_user(Role.ADMIN)
    async with TestSessionLocal() as db:
        target = await create_user(db, "deact@test.com", "deact", "password123")
        target.deactivated_by_sync = True
        await db.commit()

        result = await deactivate_user(target.id, db=db, current_user=admin)
        assert result.is_active is False

        await db.refresh(target)
        assert target.deactivated_by_sync is False


@pytest.mark.asyncio
async def test_deactivate_user_direct_not_found():
    from app.routers.admin import deactivate_user

    admin = _mock_user(Role.ADMIN)
    async with TestSessionLocal() as db:
        with pytest.raises(HTTPException) as exc:
            await deactivate_user(uuid.uuid4(), db=db, current_user=admin)
        assert exc.value.status_code == 404
        assert exc.value.detail == "User not found"


@pytest.mark.asyncio
async def test_deactivate_user_direct_cannot_deactivate_self():
    from app.routers.admin import deactivate_user

    admin_id = uuid.uuid4()
    admin = _mock_user(Role.ADMIN, user_id=admin_id)
    async with TestSessionLocal() as db:
        with pytest.raises(HTTPException) as exc:
            await deactivate_user(admin_id, db=db, current_user=admin)
        assert exc.value.status_code == 409
        assert exc.value.detail == "Cannot deactivate your own account"


# ---------------------------------------------------------------------------
# tokens router: create_token, list_tokens, delete_token, exchange_token
# ---------------------------------------------------------------------------


async def _make_principal(db, role: Role = Role.USER, *, username: str = "svc"):
    from app.services.auth_service import create_user

    return await create_user(db, f"{username}@test.com", username, "password123", role)


@pytest.mark.asyncio
async def test_create_token_direct_success_returns_raw_token_once():
    from app.routers.tokens import create_token
    from app.schemas.api_token import CreateApiTokenRequest

    admin = _mock_user(Role.ADMIN)
    async with TestSessionLocal() as db:
        principal = await _make_principal(db, Role.USER, username="principal1")
        body = CreateApiTokenRequest(name="ci-bot", principal_id=principal.id, role=Role.USER)
        result = await create_token(body, db=db, current_user=admin)
        assert result.name == "ci-bot"
        assert result.principal_id == principal.id
        assert result.token  # the raw token, shown exactly once


@pytest.mark.asyncio
async def test_create_token_direct_unknown_principal_404():
    from app.routers.tokens import create_token
    from app.schemas.api_token import CreateApiTokenRequest

    admin = _mock_user(Role.ADMIN)
    async with TestSessionLocal() as db:
        body = CreateApiTokenRequest(name="x", principal_id=uuid.uuid4(), role=Role.USER)
        with pytest.raises(HTTPException) as exc:
            await create_token(body, db=db, current_user=admin)
        assert exc.value.status_code == 404
        assert exc.value.detail == "Principal user not found"


@pytest.mark.asyncio
async def test_create_token_direct_role_exceeds_caller_403():
    from app.routers.tokens import create_token
    from app.schemas.api_token import CreateApiTokenRequest

    admin = _mock_user(Role.ADMIN)
    async with TestSessionLocal() as db:
        principal = await _make_principal(db, Role.USER, username="principal2")
        body = CreateApiTokenRequest(name="x", principal_id=principal.id, role=Role.SUPERADMIN)
        with pytest.raises(HTTPException) as exc:
            await create_token(body, db=db, current_user=admin)
        assert exc.value.status_code == 403
        assert "exceeds your own role" in exc.value.detail


@pytest.mark.asyncio
async def test_create_token_direct_principal_role_exceeds_caller_403():
    """The principal-rank axis, not just the requested-role axis: an admin
    minting a USER-role token FOR a superadmin principal must still be
    refused, since exchanging that token yields a superadmin JWT."""
    from app.routers.tokens import create_token
    from app.schemas.api_token import CreateApiTokenRequest

    admin = _mock_user(Role.ADMIN)
    async with TestSessionLocal() as db:
        superadmin_principal = await _make_principal(db, Role.SUPERADMIN, username="sa-principal")
        body = CreateApiTokenRequest(name="x", principal_id=superadmin_principal.id, role=Role.USER)
        with pytest.raises(HTTPException) as exc:
            await create_token(body, db=db, current_user=admin)
        assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_create_token_direct_service_backstop_maps_to_400(monkeypatch):
    """create_api_token's own role_exceeds check is defense in depth behind
    the router's identical pre-check (issue #312): if the router's guard is
    ever bypassed (a bug, or a future caller that skips it), the service
    still refuses and the router must map that ValueError to a 400, not let
    it propagate as a 500."""
    from app.routers import tokens as tokens_router
    from app.routers.tokens import create_token
    from app.schemas.api_token import CreateApiTokenRequest

    monkeypatch.setattr(tokens_router, "_role_exceeds", lambda requested, current: False)
    admin = _mock_user(Role.ADMIN)
    async with TestSessionLocal() as db:
        principal = await _make_principal(db, Role.USER, username="principal-backstop")
        body = CreateApiTokenRequest(name="x", principal_id=principal.id, role=Role.SUPERADMIN)
        with pytest.raises(HTTPException) as exc:
            await create_token(body, db=db, current_user=admin)
        assert exc.value.status_code == 400
        assert "cannot exceed the principal's role" in exc.value.detail


@pytest.mark.asyncio
async def test_list_tokens_direct_returns_metadata():
    from app.routers.tokens import create_token, list_tokens
    from app.schemas.api_token import CreateApiTokenRequest

    admin = _mock_user(Role.ADMIN)
    async with TestSessionLocal() as db:
        principal = await _make_principal(db, Role.USER, username="principal3")
        body = CreateApiTokenRequest(name="ci-bot", principal_id=principal.id, role=Role.USER)
        await create_token(body, db=db, current_user=admin)

        result = await list_tokens(db=db, _=admin)
        assert len(result) == 1
        assert result[0].name == "ci-bot"
        assert not hasattr(result[0], "token")  # metadata only, never the raw value


@pytest.mark.asyncio
async def test_delete_token_direct_is_idempotent():
    from app.routers.tokens import create_token, delete_token
    from app.schemas.api_token import CreateApiTokenRequest

    admin = _mock_user(Role.ADMIN)
    async with TestSessionLocal() as db:
        principal = await _make_principal(db, Role.USER, username="principal4")
        body = CreateApiTokenRequest(name="ci-bot", principal_id=principal.id, role=Role.USER)
        created = await create_token(body, db=db, current_user=admin)

        # First revoke succeeds; a second revoke of the same (or an unknown)
        # id must not raise, matching the docstring's "idempotent" contract.
        first = await delete_token(created.id, db=db, _=admin)
        assert first is None
        second = await delete_token(created.id, db=db, _=admin)
        assert second is None
        unknown = await delete_token(uuid.uuid4(), db=db, _=admin)
        assert unknown is None


@pytest.mark.asyncio
async def test_exchange_token_direct_success():
    from app.routers.tokens import create_token, exchange_token
    from app.schemas.api_token import CreateApiTokenRequest, ExchangeTokenRequest

    admin = _mock_user(Role.ADMIN)
    async with TestSessionLocal() as db:
        principal = await _make_principal(db, Role.USER, username="principal5")
        body = CreateApiTokenRequest(name="ci-bot", principal_id=principal.id, role=Role.USER)
        created = await create_token(body, db=db, current_user=admin)

        result = await exchange_token(ExchangeTokenRequest(token=created.token), db=db)
        assert result.token_type == "bearer"
        assert result.access_token
        assert result.expires_in > 0


@pytest.mark.asyncio
async def test_exchange_token_direct_unknown_token_401():
    from app.routers.tokens import exchange_token
    from app.schemas.api_token import ExchangeTokenRequest

    async with TestSessionLocal() as db:
        with pytest.raises(HTTPException) as exc:
            await exchange_token(ExchangeTokenRequest(token="not-a-real-token"), db=db)
        assert exc.value.status_code == 401
        assert exc.value.detail == "Invalid or expired token"
        assert exc.value.headers == {"WWW-Authenticate": "Bearer"}
