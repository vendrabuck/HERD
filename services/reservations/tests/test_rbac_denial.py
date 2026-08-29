"""RBAC denial sweep: require_admin endpoints in the reservations service
(admin-gated reporting endpoints) return 403 for a non-admin caller.
"""

import uuid

import pytest
from app.database import get_db
from app.dependencies.auth import get_current_user_payload
from app.main import app
from app.routers.reservations import bearer_scheme
from httpx import ASGITransport, AsyncClient

from tests._harness import override_bearer as _override_bearer
from tests._harness import override_get_db as _override_get_db


def _override_user():
    return {"sub": str(uuid.uuid4()), "username": "regular", "role": "user"}


@pytest.fixture
async def user_client():
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user_payload] = _override_user
    app.dependency_overrides[bearer_scheme] = _override_bearer
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


ADMIN_ENDPOINTS = [
    ("GET", "/reports/utilization?start=2026-01-01T00:00:00Z&end=2026-01-02T00:00:00Z"),
    ("GET", "/reports/utilization.csv?start=2026-01-01T00:00:00Z&end=2026-01-02T00:00:00Z"),
]


@pytest.mark.parametrize("method,path", ADMIN_ENDPOINTS)
@pytest.mark.asyncio
async def test_non_admin_denied(user_client, method, path):
    resp = await user_client.request(method, path)
    assert resp.status_code == 403, (method, path, resp.status_code, resp.text)
