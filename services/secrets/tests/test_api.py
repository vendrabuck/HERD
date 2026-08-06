"""API tests for the secrets service (issue #39, ADR 0003).

In-memory SQLite (StaticPool), no network: the ACL client helpers in
app.routers.secrets are monkeypatched per test. These pin the authorization
matrix (admin, manage, view, no grant), the 404-versus-403 denial semantics,
the internal-token surface, and that plaintext never leaks into metadata
responses or logs.
"""

import logging
import uuid

import pytest
from app import routers
from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models import Secret
from app.services.keyring import bootstrap_keyring
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from jose import jwt
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

SECRET_KEY = "test-secret"
PLAINTEXT = {"username": "svc-account", "password": "hunter2-plaintext-canary"}


def _token(role: str = "admin", sub: str | None = None) -> str:
    return jwt.encode(
        {"sub": sub or str(uuid.uuid4()), "role": role}, SECRET_KEY, algorithm="HS256"
    )


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def client():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _override_get_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    async with factory() as session:
        app.state.keyring = await bootstrap_keyring(session, kek_encoded=settings.secrets_kek)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
    await engine.dispose()


@pytest.fixture(autouse=True)
def _no_secret_references(monkeypatch):
    """Default the issue #456 delete guard to "no referencing hypervisors".

    Unit tests run with no inventory service; tests that exercise the guard's
    409/503 behavior override this with their own monkeypatch.
    """

    async def _none(secret_id):
        return []

    monkeypatch.setattr(routers.secrets, "find_hypervisors_referencing_secret", _none)


def _patch_grants(monkeypatch, *, view: bool = False, manage: bool = False):
    """Fake the ACL service: fixed answers per permission, no network."""

    async def fake_has_grant(payload, secret_id, permission, authorization):
        return {"view": view, "manage": manage}[permission]

    monkeypatch.setattr(routers.secrets, "_has_grant", fake_has_grant)


async def _create(client, name: str = "pve-root") -> dict:
    resp = await client.post(
        "/secrets",
        json={"name": name, "type": "password", "data": PLAINTEXT},
        headers=_auth(_token("admin")),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_admin_create_returns_metadata_only(client):
    body = await _create(client)
    assert body["name"] == "pve-root"
    assert body["type"] == "password"
    assert body["key_version"] == 1
    assert "data" not in body
    assert "hunter2-plaintext-canary" not in str(body)


async def test_duplicate_name_is_409(client):
    await _create(client)
    resp = await client.post(
        "/secrets",
        json={"name": "pve-root", "data": {"k": "v"}},
        headers=_auth(_token("admin")),
    )
    assert resp.status_code == 409


async def test_empty_data_is_422(client):
    resp = await client.post(
        "/secrets",
        json={"name": "x", "data": {}},
        headers=_auth(_token("admin")),
    )
    assert resp.status_code == 422


async def test_admin_reveal_round_trips(client):
    body = await _create(client)
    resp = await client.get(f"/secrets/{body['id']}/value", headers=_auth(_token("admin")))
    assert resp.status_code == 200
    assert resp.json()["data"] == PLAINTEXT


async def test_ciphertext_in_db_has_no_plaintext(client):
    """The issue #39 headline criterion: the database row alone yields nothing."""
    body = await _create(client)
    factory = app.dependency_overrides[get_db]
    async for session in factory():
        row = await session.get(Secret, uuid.UUID(body["id"]))
        assert b"hunter2-plaintext-canary" not in row.ciphertext
        assert b"svc-account" not in row.ciphertext


async def test_non_admin_cannot_create(client):
    resp = await client.post(
        "/secrets",
        json={"name": "x", "data": {"k": "v"}},
        headers=_auth(_token("user")),
    )
    assert resp.status_code == 403


async def test_invalid_token_is_401(client):
    resp = await client.get("/secrets", headers=_auth("not-a-jwt"))
    assert resp.status_code == 401


async def test_no_grant_get_is_404_not_403(client, monkeypatch):
    """Existence is not confirmed to a caller with no grant (inventory precedent)."""
    body = await _create(client)
    _patch_grants(monkeypatch, view=False, manage=False)
    resp = await client.get(f"/secrets/{body['id']}", headers=_auth(_token("user")))
    assert resp.status_code == 404
    resp = await client.get(f"/secrets/{body['id']}/value", headers=_auth(_token("user")))
    assert resp.status_code == 404


async def test_view_grant_sees_metadata_but_reveal_is_403(client, monkeypatch):
    body = await _create(client)
    _patch_grants(monkeypatch, view=True, manage=False)
    resp = await client.get(f"/secrets/{body['id']}", headers=_auth(_token("user")))
    assert resp.status_code == 200
    assert "data" not in resp.json()
    resp = await client.get(f"/secrets/{body['id']}/value", headers=_auth(_token("user")))
    assert resp.status_code == 403
    assert resp.json()["detail"] == "manage permission required"


async def test_manage_grant_reveals(client, monkeypatch):
    body = await _create(client)
    _patch_grants(monkeypatch, view=False, manage=True)
    resp = await client.get(f"/secrets/{body['id']}/value", headers=_auth(_token("user")))
    assert resp.status_code == 200
    assert resp.json()["data"] == PLAINTEXT


async def test_non_admin_list_is_acl_filtered(client, monkeypatch):
    visible = await _create(client, "visible")
    await _create(client, "hidden")

    async def fake_granted_ids(user_id, permission, authorization):
        return {uuid.UUID(visible["id"])} if permission == "view" else set()

    monkeypatch.setattr(routers.secrets, "_granted_secret_ids", fake_granted_ids)
    resp = await client.get("/secrets", headers=_auth(_token("user")))
    assert resp.status_code == 200
    assert [s["name"] for s in resp.json()] == ["visible"]


async def test_admin_list_sees_all(client):
    await _create(client, "a")
    await _create(client, "b")
    resp = await client.get("/secrets", headers=_auth(_token("admin")))
    assert [s["name"] for s in resp.json()] == ["a", "b"]


async def test_update_data_reencrypts(client):
    body = await _create(client)
    resp = await client.put(
        f"/secrets/{body['id']}",
        json={"data": {"password": "rotated-credential"}},
        headers=_auth(_token("admin")),
    )
    assert resp.status_code == 200
    resp = await client.get(f"/secrets/{body['id']}/value", headers=_auth(_token("admin")))
    assert resp.json()["data"] == {"password": "rotated-credential"}


async def test_delete_then_404(client):
    body = await _create(client)
    resp = await client.delete(f"/secrets/{body['id']}", headers=_auth(_token("admin")))
    assert resp.status_code == 204
    resp = await client.get(f"/secrets/{body['id']}", headers=_auth(_token("admin")))
    assert resp.status_code == 404


async def test_delete_refused_while_hypervisor_references_secret(client, monkeypatch):
    """Issue #456: the delete 409s, naming the referencing hypervisors."""
    body = await _create(client, name="hv-cred-in-use")
    hv_id = str(uuid.uuid4())
    seen: list[uuid.UUID] = []

    async def _referencing(secret_id):
        seen.append(secret_id)
        return [{"id": hv_id, "name": "Proxmox A"}]

    monkeypatch.setattr(routers.secrets, "find_hypervisors_referencing_secret", _referencing)
    resp = await client.delete(f"/secrets/{body['id']}", headers=_auth(_token("admin")))
    assert resp.status_code == 409
    assert resp.json()["detail"] == {
        "error": "secret_in_use",
        "hypervisor_ids": [hv_id],
        "hypervisor_names": ["Proxmox A"],
    }
    assert seen == [uuid.UUID(body["id"])]
    # The refused delete left the secret intact.
    resp = await client.get(f"/secrets/{body['id']}", headers=_auth(_token("admin")))
    assert resp.status_code == 200


async def test_delete_fails_closed_when_inventory_unreachable(client, monkeypatch):
    """Issue #456: an unverifiable reference check blocks the delete (503)."""
    body = await _create(client, name="hv-cred-unverifiable")

    async def _unreachable(secret_id):
        raise HTTPException(
            status_code=503,
            detail="inventory service unreachable while checking secret references",
        )

    monkeypatch.setattr(routers.secrets, "find_hypervisors_referencing_secret", _unreachable)
    resp = await client.delete(f"/secrets/{body['id']}", headers=_auth(_token("admin")))
    assert resp.status_code == 503
    assert resp.json()["detail"] == (
        "inventory service unreachable while checking secret references"
    )
    # Fail-closed means blocked, not deleted.
    resp = await client.get(f"/secrets/{body['id']}", headers=_auth(_token("admin")))
    assert resp.status_code == 200


async def test_rotate_endpoint(client):
    body = await _create(client)
    resp = await client.post("/keys/rotate", headers=_auth(_token("admin")))
    assert resp.status_code == 200
    assert resp.json() == {"new_version": 2, "reencrypted": 1}
    resp = await client.get(f"/secrets/{body['id']}/value", headers=_auth(_token("admin")))
    assert resp.json()["data"] == PLAINTEXT


async def test_rotate_requires_admin(client):
    resp = await client.post("/keys/rotate", headers=_auth(_token("user")))
    assert resp.status_code == 403


async def test_internal_reveal_by_id(client):
    body = await _create(client)
    resp = await client.get(
        f"/internal/secrets/{body['id']}/value",
        headers={"X-Internal-Token": "test-token"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"] == PLAINTEXT


async def test_internal_reveal_by_name(client):
    await _create(client, "frr-router-ssh")
    resp = await client.get(
        "/internal/secrets/by-name/frr-router-ssh/value",
        headers={"X-Internal-Token": "test-token"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"] == PLAINTEXT


async def test_internal_wrong_token_is_403(client):
    body = await _create(client)
    for headers in ({"X-Internal-Token": "wrong"}, {}):
        resp = await client.get(f"/internal/secrets/{body['id']}/value", headers=headers)
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Invalid internal token"


async def test_internal_unknown_secret_is_404(client):
    resp = await client.get(
        f"/internal/secrets/{uuid.uuid4()}/value",
        headers={"X-Internal-Token": "test-token"},
    )
    assert resp.status_code == 404


async def test_plaintext_never_hits_the_logs(client, caplog):
    """Create, list, reveal, and rotate with DEBUG logging on: the plaintext
    canary must not appear in any log record."""
    with caplog.at_level(logging.DEBUG):
        body = await _create(client)
        await client.get("/secrets", headers=_auth(_token("admin")))
        resp = await client.get(f"/secrets/{body['id']}/value", headers=_auth(_token("admin")))
        assert resp.status_code == 200
        await client.post("/keys/rotate", headers=_auth(_token("admin")))
    assert "hunter2-plaintext-canary" not in caplog.text
    assert "svc-account" not in caplog.text
