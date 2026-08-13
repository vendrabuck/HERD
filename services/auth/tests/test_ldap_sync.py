"""Unit tests for the ldap-sync router (ADR 0011 phases 2 and 3).

The directory client is mocked at the ldap_service module boundary; its own
behavior against a real directory is covered by test_ldap_service_live.py.
What is pinned here is the router's proof discipline: 422 only when the
directory PROVED the DN unresolvable, 503 when it could not be asked (an
outage must never read as a bad DN), and the accept-with-warning rule for
entries lacking the member attribute (decision 2026-08-12). The FK cascade
(HERD group delete removes the mapping) is declared in migration 0006 and
enforced by Postgres; SQLite runs without FK enforcement here, so that path
is deliberately not asserted in this file.

The phase 3 sync-now tests exercise only the in-process asyncio-lock layer
of the run serialization (owned by ldap_sync_service, S1): the Postgres
advisory-lock layer is gated on dialect == "postgresql" and SQLite has no
advisory locks. The background task's session factory is pointed at the
test engine so the run it finalizes is the same one the GET endpoints read.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from app import database
from app.config import settings
from app.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.main import app
from app.models.ldap_sync_run import LdapSyncRun
from app.models.user import Role, User
from app.routers.ldap_sync import NO_MEMBERS_WARNING
from app.services import ldap_service, ldap_sync_service
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

engine = create_async_engine(TEST_DATABASE_URL, echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

_GROUP_DN = "cn=herd-eng,ou=groups,dc=company,dc=local"


async def override_get_db():
    async with TestSessionLocal() as session:
        yield session


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def ldap_mode(monkeypatch):
    monkeypatch.setattr(settings, "auth_method", "ldap", raising=False)


def _make_mock_user(role: Role, username: str) -> User:
    return User(
        id=uuid.uuid4(),
        email=f"{username}@test.com",
        username=username,
        hashed_password="fake",
        is_active=True,
        role=role,
    )


def _client_for(user: User | None):
    app.dependency_overrides[get_db] = override_get_db
    if user is not None:
        app.dependency_overrides[get_current_user] = lambda: user
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def admin_client():
    async with _client_for(_make_mock_user(Role.ADMIN, "admin")) as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def user_client():
    async with _client_for(_make_mock_user(Role.USER, "regular")) as ac:
        yield ac
    app.dependency_overrides.clear()


def _stub_fetch_group(monkeypatch, *, entry=..., error=None):
    async def fake(group_dn: str):
        if error is not None:
            raise error
        if entry is ...:
            return ldap_service.LdapGroupEntry(
                dn=group_dn,
                name="herd-eng",
                member_dns=("uid=user1,ou=people,dc=company,dc=local",),
            )
        return entry

    monkeypatch.setattr(ldap_service, "fetch_group", fake)


async def _create_herd_group(client, name="Engineering") -> str:
    resp = await client.post("/groups", json={"name": name, "description": None})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _create_mapping(client, group_id: str, group_dn: str = _GROUP_DN):
    return await client.post(
        "/admin/ldap-sync/mappings",
        json={"group_dn": group_dn, "herd_group_id": group_id},
    )


@pytest.mark.asyncio
async def test_create_mapping_success_caches_directory_name(monkeypatch, admin_client):
    _stub_fetch_group(monkeypatch)
    group_id = await _create_herd_group(admin_client)
    resp = await _create_mapping(admin_client, group_id)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["group_dn"] == _GROUP_DN
    assert body["directory_name"] == "herd-eng"
    assert body["herd_group_id"] == group_id
    assert body["warning"] is None


@pytest.mark.asyncio
async def test_create_mapping_with_no_members_warns_but_succeeds(monkeypatch, admin_client):
    # The accept-with-warning decision: an AD-style empty group (or a typo'd
    # non-group entry) maps successfully and the response says why to look.
    # Emptiness of member_dns is the trigger; ldap3 back-fills missing
    # attributes as empty, so attribute absence is not observable separately.
    entry = ldap_service.LdapGroupEntry(dn=_GROUP_DN, name="herd-eng", member_dns=())
    _stub_fetch_group(monkeypatch, entry=entry)
    group_id = await _create_herd_group(admin_client)
    resp = await _create_mapping(admin_client, group_id)
    assert resp.status_code == 201, resp.text
    assert resp.json()["warning"] == NO_MEMBERS_WARNING


@pytest.mark.asyncio
async def test_create_mapping_stores_canonical_directory_dn(monkeypatch, admin_client):
    # DNs are case-insensitive; persisting the admin-typed form would let
    # case variants of one directory group bypass the unique constraint.
    canonical = _GROUP_DN
    entry = ldap_service.LdapGroupEntry(
        dn=canonical, name="herd-eng", member_dns=("uid=user1,ou=people,dc=company,dc=local",)
    )
    _stub_fetch_group(monkeypatch, entry=entry)
    group_id = await _create_herd_group(admin_client)
    resp = await _create_mapping(admin_client, group_id, group_dn=canonical.upper())
    assert resp.status_code == 201, resp.text
    assert resp.json()["group_dn"] == canonical


@pytest.mark.asyncio
async def test_create_mapping_herd_group_already_mapped_is_409(monkeypatch, admin_client):
    # One directory group per HERD group: the reconciler's per-mapping set
    # arithmetic cannot converge when two mappings fight over one group.
    _stub_fetch_group(monkeypatch)
    group_id = await _create_herd_group(admin_client)
    first = await _create_mapping(admin_client, group_id)
    assert first.status_code == 201
    resp = await _create_mapping(
        admin_client, group_id, group_dn="cn=other,ou=groups,dc=company,dc=local"
    )
    assert resp.status_code == 409
    assert "already has a directory mapping" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_mapping_truncates_long_directory_name(monkeypatch, admin_client):
    # directory_name is a display cache capped at 255; an unbounded directory
    # value (or the DN fallback) must not 500 the insert.
    entry = ldap_service.LdapGroupEntry(dn=_GROUP_DN, name="x" * 400, member_dns=("m",))
    _stub_fetch_group(monkeypatch, entry=entry)
    group_id = await _create_herd_group(admin_client)
    resp = await _create_mapping(admin_client, group_id)
    assert resp.status_code == 201, resp.text
    assert len(resp.json()["directory_name"]) == 255


@pytest.mark.asyncio
async def test_create_mapping_dangling_dn_is_422(monkeypatch, admin_client):
    _stub_fetch_group(monkeypatch, entry=None)
    group_id = await _create_herd_group(admin_client)
    resp = await _create_mapping(admin_client, group_id)
    assert resp.status_code == 422
    assert "does not resolve" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_mapping_directory_outage_is_503_not_422(monkeypatch, admin_client):
    # Error is never absence: an unreachable directory must not condemn the DN.
    _stub_fetch_group(
        monkeypatch, error=ldap_service.LdapUnavailableError("service-account bind failed")
    )
    group_id = await _create_herd_group(admin_client)
    resp = await _create_mapping(admin_client, group_id)
    assert resp.status_code == 503
    assert "not validated" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_mapping_duplicate_dn_is_409(monkeypatch, admin_client):
    _stub_fetch_group(monkeypatch)
    group_id = await _create_herd_group(admin_client)
    other_id = await _create_herd_group(admin_client, name="Other")
    assert (await _create_mapping(admin_client, group_id)).status_code == 201
    resp = await _create_mapping(admin_client, other_id)
    assert resp.status_code == 409
    assert "already exists" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_mapping_unknown_herd_group_is_404(monkeypatch, admin_client):
    _stub_fetch_group(monkeypatch)
    resp = await _create_mapping(admin_client, str(uuid.uuid4()))
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_create_mapping_refused_outside_ldap_mode(monkeypatch, admin_client):
    monkeypatch.setattr(settings, "auth_method", "local", raising=False)
    _stub_fetch_group(monkeypatch)
    group_id = await _create_herd_group(admin_client)
    resp = await _create_mapping(admin_client, group_id)
    assert resp.status_code == 409
    assert "auth_method" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_list_and_delete_work_outside_ldap_mode(monkeypatch, admin_client):
    # Cleanup must not require a reachable (or configured) directory.
    _stub_fetch_group(monkeypatch)
    group_id = await _create_herd_group(admin_client)
    created = await _create_mapping(admin_client, group_id)
    mapping_id = created.json()["id"]
    monkeypatch.setattr(settings, "auth_method", "local", raising=False)
    listed = await admin_client.get("/admin/ldap-sync/mappings")
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    deleted = await admin_client.delete(f"/admin/ldap-sync/mappings/{mapping_id}")
    assert deleted.status_code == 204


@pytest.mark.asyncio
async def test_list_mappings_paginates(monkeypatch, admin_client):
    _stub_fetch_group(monkeypatch)
    for i in range(3):
        group_id = await _create_herd_group(admin_client, name=f"Team {i}")
        resp = await _create_mapping(
            admin_client, group_id, group_dn=f"cn=g{i},ou=groups,dc=company,dc=local"
        )
        assert resp.status_code == 201
    page = await admin_client.get("/admin/ldap-sync/mappings", params={"skip": 1, "limit": 1})
    assert page.status_code == 200
    body = page.json()
    assert body["total"] == 3
    assert len(body["items"]) == 1
    assert body["skip"] == 1 and body["limit"] == 1


@pytest.mark.asyncio
async def test_delete_missing_mapping_is_404(admin_client):
    resp = await admin_client.delete(f"/admin/ldap-sync/mappings/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_non_admin_is_403_on_all_mapping_endpoints(monkeypatch, user_client):
    _stub_fetch_group(monkeypatch)
    create = await user_client.post(
        "/admin/ldap-sync/mappings",
        json={"group_dn": _GROUP_DN, "herd_group_id": str(uuid.uuid4())},
    )
    listed = await user_client.get("/admin/ldap-sync/mappings")
    deleted = await user_client.delete(f"/admin/ldap-sync/mappings/{uuid.uuid4()}")
    assert (create.status_code, listed.status_code, deleted.status_code) == (403, 403, 403)


@pytest.mark.asyncio
async def test_unauthenticated_is_401():
    try:
        async with _client_for(None) as ac:
            resp = await ac.get("/admin/ldap-sync/mappings")
        assert resp.status_code == 401
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Sync-now endpoint and run history (phase 3).
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def background_session(monkeypatch):
    # The background task opens its own session from database.AsyncSessionLocal
    # (never the request's); point it at the test engine so it acts on the
    # same database the request and GET sessions use.
    monkeypatch.setattr(database, "AsyncSessionLocal", TestSessionLocal)


async def _await_background() -> None:
    # S4: the single drain implementation, used by both the autouse fixture
    # (teardown) and tests that need to await a scheduled run mid-test.
    # _background_tasks and _sync_lock now live on ldap_sync_service (S1),
    # not the router.
    for task in list(ldap_sync_service._background_tasks):
        await asyncio.wait_for(task, timeout=5)


@pytest.fixture(autouse=True)
async def drain_background_tasks():
    # Teardown ordering matters: this fixture is defined after setup_db, so
    # it finalizes first and no background task outlives the tables.
    yield
    await _await_background()
    assert not ldap_sync_service._sync_lock.locked()


@pytest.mark.asyncio
async def test_sync_run_202_then_run_visible_and_finalized(admin_client):
    # Zero mappings: the REAL reconciler runs end to end through the
    # background wiring without touching the directory client.
    resp = await admin_client.post("/admin/ldap-sync/run")
    assert resp.status_code == 202, resp.text
    run_id = resp.json()["run_id"]
    await _await_background()

    single = await admin_client.get(f"/admin/ldap-sync/runs/{run_id}")
    assert single.status_code == 200
    body = single.json()
    assert body["status"] == "success"
    assert body["trigger"] == "manual"
    assert body["finished_at"] is not None
    assert (body["members_added"], body["members_removed"], body["users_provisioned"]) == (0, 0, 0)

    listed = await admin_client.get("/admin/ldap-sync/runs")
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["id"] == run_id


@pytest.mark.asyncio
async def test_sync_run_409_while_in_progress_and_lock_released_after(monkeypatch, admin_client):
    gate = asyncio.Event()

    async def blocking_execute_run(session, run):
        # Await the gate BEFORE any session use so the blocked task holds no
        # open transaction on the shared SQLite connection.
        await gate.wait()
        run.status = "success"
        run.finished_at = datetime.now(timezone.utc)
        await session.commit()
        return run

    monkeypatch.setattr(ldap_sync_service, "execute_run", blocking_execute_run)
    try:
        first = await admin_client.post("/admin/ldap-sync/run")
        assert first.status_code == 202
        second = await admin_client.post("/admin/ldap-sync/run")
        assert second.status_code == 409
        assert "in progress" in second.json()["detail"]
    finally:
        gate.set()
    await _await_background()

    # The lock is released by the background task's finally, never leaked:
    # a third run starts cleanly.
    third = await admin_client.post("/admin/ldap-sync/run")
    assert third.status_code == 202
    assert third.json()["run_id"] != first.json()["run_id"]


@pytest.mark.asyncio
async def test_sync_run_refused_outside_ldap_mode_without_leaking_lock(monkeypatch, admin_client):
    monkeypatch.setattr(settings, "auth_method", "local", raising=False)
    resp = await admin_client.post("/admin/ldap-sync/run")
    assert resp.status_code == 409
    assert "auth_method" in resp.json()["detail"]
    assert not ldap_sync_service._sync_lock.locked()


@pytest.mark.asyncio
async def test_non_admin_is_403_on_all_run_endpoints(user_client):
    post = await user_client.post("/admin/ldap-sync/run")
    listed = await user_client.get("/admin/ldap-sync/runs")
    single = await user_client.get(f"/admin/ldap-sync/runs/{uuid.uuid4()}")
    assert (post.status_code, listed.status_code, single.status_code) == (403, 403, 403)


@pytest.mark.asyncio
async def test_list_runs_paginates_newest_first(admin_client):
    base = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)
    ids = []
    async with TestSessionLocal() as session:
        for i in range(3):
            # Explicit id: the model's uuid4 default applies at flush, so
            # reading run.id before commit would yield None.
            run = LdapSyncRun(
                id=uuid.uuid4(),
                trigger="manual",
                status="success",
                started_at=base + timedelta(minutes=i),
                finished_at=base + timedelta(minutes=i, seconds=30),
            )
            session.add(run)
            ids.append(str(run.id))
        await session.commit()

    page = await admin_client.get("/admin/ldap-sync/runs", params={"skip": 0, "limit": 2})
    assert page.status_code == 200
    body = page.json()
    assert body["total"] == 3
    assert [item["id"] for item in body["items"]] == [ids[2], ids[1]]

    rest = await admin_client.get("/admin/ldap-sync/runs", params={"skip": 2, "limit": 2})
    assert [item["id"] for item in rest.json()["items"]] == [ids[0]]


@pytest.mark.asyncio
async def test_get_missing_run_is_404(admin_client):
    resp = await admin_client.get(f"/admin/ldap-sync/runs/{uuid.uuid4()}")
    assert resp.status_code == 404
