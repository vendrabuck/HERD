"""Direct route-handler tests for app/routers/grants.py.

coverage.py's sys.monitoring core under-reports lines that execute after the
first `await` inside an async endpoint when the endpoint is exercised only
through httpx.ASGITransport (see reference-herd-async-coverage-artifact): the
line runs, a passing test asserts on it, and it still shows up as "missing".
This file calls the router handler functions directly (no ASGITransport,
mirroring services/cabling/tests/test_route_handlers_direct.py) so pytest-cov
credits the create-201/duplicate-409/list/get-404/delete-404/delete-204
bodies that test_grants.py and test_grants_errors.py already exercise over
HTTP but which the tracer loses under the ASGI path.
"""

import uuid

import pytest
from app.database import Base
from app.models.grant import ResourceGrant
from app.routers.grants import (
    create_grant_endpoint,
    delete_grant_endpoint,
    get_grant_endpoint,
    list_grants_endpoint,
)
from app.schemas.grant import GrantCreateRequest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSession = async_sessionmaker(engine, expire_on_commit=False)

_admin_id = uuid.uuid4()
_group_id = uuid.uuid4()
_device_id = uuid.uuid4()


def _admin_payload():
    return {"sub": str(_admin_id), "role": "admin"}


def _grant_body(permission="view", resource_id=None):
    return GrantCreateRequest(
        group_id=_group_id,
        resource_type="device",
        resource_id=resource_id or _device_id,
        permission=permission,
    )


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.mark.asyncio
async def test_create_grant_endpoint_returns_grant_with_granted_by():
    async with TestSession() as db:
        grant = await create_grant_endpoint(
            body=_grant_body(),
            db=db,
            payload=_admin_payload(),
        )
    assert grant.group_id == _group_id
    assert grant.resource_type == "device"
    assert grant.resource_id == _device_id
    assert grant.permission == "view"
    assert grant.granted_by == _admin_id
    assert grant.id is not None


@pytest.mark.asyncio
async def test_create_grant_endpoint_duplicate_raises_409():
    async with TestSession() as db:
        await create_grant_endpoint(body=_grant_body(), db=db, payload=_admin_payload())

    async with TestSession() as db:
        with pytest.raises(HTTPException) as exc:
            await create_grant_endpoint(body=_grant_body(), db=db, payload=_admin_payload())
    assert exc.value.status_code == 409
    assert exc.value.detail == "This grant already exists"


@pytest.mark.asyncio
async def test_list_grants_endpoint_returns_paginated_body():
    async with TestSession() as db:
        await create_grant_endpoint(
            body=_grant_body(permission="view"), db=db, payload=_admin_payload()
        )
        await create_grant_endpoint(
            body=_grant_body(permission="manage"), db=db, payload=_admin_payload()
        )

    async with TestSession() as db:
        page = await list_grants_endpoint(
            group_id=None,
            resource_type=None,
            resource_id=None,
            skip=1,
            limit=1,
            db=db,
            _=_admin_payload(),
        )
    assert page.total == 2
    assert page.skip == 1
    assert page.limit == 1
    assert len(page.items) == 1


@pytest.mark.asyncio
async def test_get_grant_endpoint_returns_grant():
    async with TestSession() as db:
        created = await create_grant_endpoint(body=_grant_body(), db=db, payload=_admin_payload())
        grant_id = created.id

    async with TestSession() as db:
        got = await get_grant_endpoint(grant_id=grant_id, db=db, _=_admin_payload())
    assert got.id == grant_id


@pytest.mark.asyncio
async def test_get_grant_endpoint_missing_raises_404():
    async with TestSession() as db:
        with pytest.raises(HTTPException) as exc:
            await get_grant_endpoint(grant_id=uuid.uuid4(), db=db, _=_admin_payload())
    assert exc.value.status_code == 404
    assert exc.value.detail == "Grant not found"


@pytest.mark.asyncio
async def test_delete_grant_endpoint_missing_raises_404():
    async with TestSession() as db:
        with pytest.raises(HTTPException) as exc:
            await delete_grant_endpoint(grant_id=uuid.uuid4(), db=db, payload=_admin_payload())
    assert exc.value.status_code == 404
    assert exc.value.detail == "Grant not found"


@pytest.mark.asyncio
async def test_delete_grant_endpoint_removes_row():
    async with TestSession() as db:
        created = await create_grant_endpoint(body=_grant_body(), db=db, payload=_admin_payload())
        grant_id = created.id

    async with TestSession() as db:
        result = await delete_grant_endpoint(grant_id=grant_id, db=db, payload=_admin_payload())
    assert result is None

    async with TestSession() as db:
        remaining = await db.get(ResourceGrant, grant_id)
    assert remaining is None
