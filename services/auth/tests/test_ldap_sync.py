"""Unit tests for the ldap-sync mapping router (ADR 0011 phase 2).

The directory client is mocked at the ldap_service module boundary; its own
behavior against a real directory is covered by test_ldap_service_live.py.
What is pinned here is the router's proof discipline: 422 only when the
directory PROVED the DN unresolvable, 503 when it could not be asked (an
outage must never read as a bad DN), and the accept-with-warning rule for
entries lacking the member attribute (decision 2026-08-12). The FK cascade
(HERD group delete removes the mapping) is declared in migration 0006 and
enforced by Postgres; SQLite runs without FK enforcement here, so that path
is deliberately not asserted in this file.
"""

import uuid

import pytest
from app.config import settings
from app.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.main import app
from app.models.user import Role, User
from app.routers.ldap_sync import MISSING_MEMBER_ATTRIBUTE_WARNING
from app.services import ldap_service
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
                member_attribute_present=True,
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
async def test_create_mapping_without_member_attribute_warns_but_succeeds(
    monkeypatch, admin_client
):
    # The accept-with-warning decision: an AD-style empty group (or a typo'd
    # non-group entry) maps successfully and the response says why to look.
    entry = ldap_service.LdapGroupEntry(
        dn=_GROUP_DN, name="herd-eng", member_dns=(), member_attribute_present=False
    )
    _stub_fetch_group(monkeypatch, entry=entry)
    group_id = await _create_herd_group(admin_client)
    resp = await _create_mapping(admin_client, group_id)
    assert resp.status_code == 201, resp.text
    assert resp.json()["warning"] == MISSING_MEMBER_ATTRIBUTE_WARNING


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
    group_id = await _create_herd_group(admin_client)
    for i in range(3):
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
    app.dependency_overrides[get_db] = override_get_db
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/admin/ldap-sync/mappings")
        assert resp.status_code == 401
    finally:
        app.dependency_overrides.clear()
