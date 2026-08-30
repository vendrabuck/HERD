"""Direct router-function tests for coverage of post-await code paths.

pytest-cov's tracer under-attributes lines that run after the first `await`
in an async endpoint, and lines that run inside SQLAlchemy's async greenlet
when the service is exercised over httpx's ASGITransport (see the module
docstring convention in services/auth/tests/test_routers_direct.py and
services/cabling/tests/test_route_handlers_direct.py). test_api.py already
pins the observable behavior of every endpoint through the ASGI client;
these tests call the same router functions directly with a real in-memory
DB session so coverage.py can trace the lines test_api.py already exercises
behaviorally, plus a few branches (`_principal_id`'s bad-sub path,
`_granted_secret_ids`'s and `_has_grant`'s real HTTP bodies) that test_api.py
does not reach because it monkeypatches those helpers.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from app.config import settings
from app.database import Base
from app.models import Secret
from app.routers.secrets import (
    _granted_secret_ids,
    _has_grant,
    _principal_id,
    create_secret,
    delete_secret,
    get_secret,
    list_secrets,
    reveal_secret,
    rotate_keys,
    update_secret,
)
from app.schemas.secret import SecretCreate, SecretUpdate
from app.services.keyring import bootstrap_keyring
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

PLAINTEXT = {"username": "svc-account", "password": "hunter2-plaintext-canary"}


@pytest.fixture
async def db_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
async def keyring(db_session):
    return await bootstrap_keyring(db_session, kek_encoded=settings.secrets_kek)


def _request(keyring) -> MagicMock:
    request = MagicMock()
    request.app.state.keyring = keyring
    return request


def _payload(role: str = "admin", sub: str | None = None) -> dict:
    return {"sub": sub or str(uuid.uuid4()), "role": role}


async def _create_secret(db_session, keyring, name: str = "pve-root") -> Secret:
    body = SecretCreate(name=name, type="password", data=PLAINTEXT)
    result = await create_secret(body, _request(keyring), _payload(), db_session)
    return await db_session.get(Secret, result.id)


# --- _principal_id ---


def test_principal_id_parses_valid_uuid():
    sub = str(uuid.uuid4())
    assert _principal_id({"sub": sub}) == uuid.UUID(sub)


def test_principal_id_none_when_sub_missing():
    assert _principal_id({}) is None


def test_principal_id_none_when_sub_not_a_uuid():
    assert _principal_id({"sub": "not-a-uuid"}) is None


# --- create_secret ---


async def test_create_secret_direct_returns_metadata(db_session, keyring):
    body = SecretCreate(name="direct-create", type="password", data=PLAINTEXT)
    result = await create_secret(body, _request(keyring), _payload(), db_session)
    assert result.name == "direct-create"
    assert result.key_version == 1
    row = await db_session.get(Secret, result.id)
    assert row.ciphertext != b""


async def test_create_secret_direct_duplicate_name_is_409(db_session, keyring):
    await _create_secret(db_session, keyring, "dup")
    body = SecretCreate(name="dup", type="password", data=PLAINTEXT)
    with pytest.raises(HTTPException) as exc:
        await create_secret(body, _request(keyring), _payload(), db_session)
    assert exc.value.status_code == 409
    assert exc.value.detail == "A secret with this name already exists"


# --- list_secrets ---


async def test_list_secrets_direct_admin_sees_all(db_session, keyring):
    await _create_secret(db_session, keyring, "a")
    await _create_secret(db_session, keyring, "b")
    result = await list_secrets(_payload(role="admin"), db_session, authorization="")
    assert [s.name for s in result] == ["a", "b"]


async def test_list_secrets_direct_non_admin_filtered_by_acl(db_session, keyring):
    visible = await _create_secret(db_session, keyring, "visible")
    await _create_secret(db_session, keyring, "hidden")
    payload = _payload(role="user")

    async def fake_granted(user_id, permission, authorization):
        return {visible.id} if permission == "view" else set()

    with patch("app.routers.secrets._granted_secret_ids", new=fake_granted):
        result = await list_secrets(payload, db_session, authorization="Bearer x")
    assert [s.name for s in result] == ["visible"]


# --- get_secret ---


async def test_get_secret_direct_not_found(db_session):
    with pytest.raises(HTTPException) as exc:
        await get_secret(uuid.uuid4(), _payload(role="admin"), db_session, authorization="")
    assert exc.value.status_code == 404
    assert exc.value.detail == "Secret not found"


async def test_get_secret_direct_admin_sees_metadata(db_session, keyring):
    row = await _create_secret(db_session, keyring)
    result = await get_secret(row.id, _payload(role="admin"), db_session, authorization="")
    assert result.name == row.name


async def test_get_secret_direct_non_admin_no_grant_is_404(db_session, keyring):
    row = await _create_secret(db_session, keyring)

    async def deny(payload, secret_id, permission, authorization):
        return False

    with patch("app.routers.secrets._has_grant", new=deny):
        with pytest.raises(HTTPException) as exc:
            await get_secret(row.id, _payload(role="user"), db_session, authorization="Bearer x")
    assert exc.value.status_code == 404


async def test_get_secret_direct_non_admin_with_grant_sees_metadata(db_session, keyring):
    row = await _create_secret(db_session, keyring)

    async def allow(payload, secret_id, permission, authorization):
        return permission == "view"

    with patch("app.routers.secrets._has_grant", new=allow):
        result = await get_secret(
            row.id, _payload(role="user"), db_session, authorization="Bearer x"
        )
    assert result.name == row.name


# --- reveal_secret ---


async def test_reveal_secret_direct_not_found(db_session, keyring):
    with pytest.raises(HTTPException) as exc:
        await reveal_secret(
            uuid.uuid4(), _request(keyring), _payload(role="admin"), db_session, authorization=""
        )
    assert exc.value.status_code == 404


async def test_reveal_secret_direct_admin_gets_plaintext(db_session, keyring):
    row = await _create_secret(db_session, keyring)
    result = await reveal_secret(
        row.id, _request(keyring), _payload(role="admin"), db_session, authorization=""
    )
    assert result.data == PLAINTEXT


async def test_reveal_secret_direct_view_only_is_403(db_session, keyring):
    row = await _create_secret(db_session, keyring)

    async def view_only(payload, secret_id, permission, authorization):
        return permission == "view"

    with patch("app.routers.secrets._has_grant", new=view_only):
        with pytest.raises(HTTPException) as exc:
            await reveal_secret(
                row.id,
                _request(keyring),
                _payload(role="user"),
                db_session,
                authorization="Bearer x",
            )
    assert exc.value.status_code == 403
    assert exc.value.detail == "manage permission required"


async def test_reveal_secret_direct_no_grant_is_404(db_session, keyring):
    row = await _create_secret(db_session, keyring)

    async def deny(payload, secret_id, permission, authorization):
        return False

    with patch("app.routers.secrets._has_grant", new=deny):
        with pytest.raises(HTTPException) as exc:
            await reveal_secret(
                row.id,
                _request(keyring),
                _payload(role="user"),
                db_session,
                authorization="Bearer x",
            )
    assert exc.value.status_code == 404


async def test_reveal_secret_direct_manage_grant_gets_plaintext(db_session, keyring):
    row = await _create_secret(db_session, keyring)

    async def manage_only(payload, secret_id, permission, authorization):
        return permission == "manage"

    with patch("app.routers.secrets._has_grant", new=manage_only):
        result = await reveal_secret(
            row.id, _request(keyring), _payload(role="user"), db_session, authorization="Bearer x"
        )
    assert result.data == PLAINTEXT


# --- update_secret ---


async def test_update_secret_direct_not_found(db_session, keyring):
    body = SecretUpdate(data={"a": "b"})
    with pytest.raises(HTTPException) as exc:
        await update_secret(uuid.uuid4(), body, _request(keyring), _payload(), db_session)
    assert exc.value.status_code == 404


async def test_update_secret_direct_reencrypts_and_updates_metadata(db_session, keyring):
    row = await _create_secret(db_session, keyring)
    body = SecretUpdate(type="token", description="rotated", data={"password": "new-credential"})
    result = await update_secret(row.id, body, _request(keyring), _payload(), db_session)
    assert result.type == "token"
    assert result.description == "rotated"
    revealed = await reveal_secret(
        row.id, _request(keyring), _payload(role="admin"), db_session, authorization=""
    )
    assert revealed.data == {"password": "new-credential"}


async def test_update_secret_direct_no_data_leaves_ciphertext_untouched(db_session, keyring):
    row = await _create_secret(db_session, keyring)
    original_nonce = row.nonce
    body = SecretUpdate(description="metadata only")
    result = await update_secret(row.id, body, _request(keyring), _payload(), db_session)
    assert result.description == "metadata only"
    refreshed = await db_session.get(Secret, row.id)
    assert refreshed.nonce == original_nonce


# --- delete_secret ---


async def test_delete_secret_direct_not_found(db_session):
    with pytest.raises(HTTPException) as exc:
        await delete_secret(uuid.uuid4(), _payload(), db_session)
    assert exc.value.status_code == 404


async def test_delete_secret_direct_removes_row(db_session, keyring):
    row = await _create_secret(db_session, keyring)
    with patch(
        "app.routers.secrets.find_hypervisors_referencing_secret", new=AsyncMock(return_value=[])
    ):
        response = await delete_secret(row.id, _payload(), db_session)
    assert response.status_code == 204
    assert await db_session.get(Secret, row.id) is None


async def test_delete_secret_direct_refused_while_referenced(db_session, keyring):
    row = await _create_secret(db_session, keyring)
    hv_id = str(uuid.uuid4())
    refs = AsyncMock(return_value=[{"id": hv_id, "name": "Proxmox A"}])
    with patch("app.routers.secrets.find_hypervisors_referencing_secret", new=refs):
        with pytest.raises(HTTPException) as exc:
            await delete_secret(row.id, _payload(), db_session)
    assert exc.value.status_code == 409
    assert exc.value.detail == {
        "error": "secret_in_use",
        "hypervisor_ids": [hv_id],
        "hypervisor_names": ["Proxmox A"],
    }
    # Refused delete left the row intact.
    assert await db_session.get(Secret, row.id) is not None


# --- rotate_keys ---


async def test_rotate_keys_direct_reencrypts_all(db_session, keyring):
    await _create_secret(db_session, keyring, "one")
    await _create_secret(db_session, keyring, "two")
    result = await rotate_keys(_request(keyring), _payload(), db_session)
    assert result.new_version == 2
    assert result.reencrypted == 2


# --- _granted_secret_ids: real HTTP body (test_api.py monkeypatches this) ---

_ACL_GET = "app.routers.secrets.httpx.AsyncClient.get"


async def test_granted_secret_ids_success_parses_resource_ids():
    rid = str(uuid.uuid4())
    mock_get = AsyncMock(return_value=httpx.Response(200, json=[{"resource_id": rid}]))
    with patch(_ACL_GET, new=mock_get):
        result = await _granted_secret_ids("user-1", "view", "Bearer x")
    assert result == {uuid.UUID(rid)}
    # Hits the ACL /resources endpoint with the expected query shape.
    _, kwargs = mock_get.call_args
    assert kwargs["params"] == {
        "user_id": "user-1",
        "resource_type": "secret",
        "permission": "view",
    }
    assert kwargs["headers"] == {"Authorization": "Bearer x"}


async def test_granted_secret_ids_transport_error_returns_empty_set():
    with patch(_ACL_GET, new=AsyncMock(side_effect=httpx.ConnectError("boom"))):
        result = await _granted_secret_ids("user-1", "view", "Bearer x")
    assert result == set()


async def test_granted_secret_ids_non_200_returns_empty_set():
    with patch(_ACL_GET, new=AsyncMock(return_value=httpx.Response(500))):
        result = await _granted_secret_ids("user-1", "view", "Bearer x")
    assert result == set()


async def test_granted_secret_ids_malformed_body_returns_empty_set():
    """A 200 with a body that doesn't parse as resource_id UUIDs fails closed."""
    with patch(_ACL_GET, new=AsyncMock(return_value=httpx.Response(200, json=[{"oops": "no-id"}]))):
        result = await _granted_secret_ids("user-1", "view", "Bearer x")
    assert result == set()


# --- _has_grant: proves delegation to herd_common.acl.user_has_grant ---


async def test_has_grant_delegates_to_shared_acl_helper():
    secret_id = uuid.uuid4()
    payload = {"sub": "user-1"}
    fake = AsyncMock(return_value=True)
    with patch("app.routers.secrets.user_has_grant", new=fake):
        result = await _has_grant(payload, secret_id, "manage", "Bearer x")
    assert result is True
    fake.assert_awaited_once_with(
        user_id="user-1",
        resource_type="secret",
        resource_id=str(secret_id),
        permission="manage",
        authorization="Bearer x",
        acl_service_url=settings.acl_service_url,
    )


async def test_has_grant_false_when_shared_helper_denies():
    with patch("app.routers.secrets.user_has_grant", new=AsyncMock(return_value=False)):
        result = await _has_grant({"sub": "user-1"}, uuid.uuid4(), "view", "Bearer x")
    assert result is False
