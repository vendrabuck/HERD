"""Driver rows and stored archives stay consistent across a replace or a storage failure.

Three behaviors on the create and replace paths that no test pinned before the
#1327 review, run through the ASGI app on the real ``app.storage`` local backend
(a temporary directory), so the assertions read the files themselves:

- a file replace that keeps its file name stores the new archive under the same
  key and does not then delete it as "the old object" (INV-DRV-12);
- a storage failure on create commits no row (INV-DRV-20);
- a storage failure on replace leaves the row naming the old archive, which is
  still stored (INV-DRV-20).
"""

import hashlib
import io
from unittest.mock import patch

import pytest
from app import storage
from app.database import Base, get_db
from app.dependencies.auth import get_current_user_payload
from app.main import app
from app.models.driver_package import DriverPackage
from app.services import driver_service
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

V1 = b"PK\x03\x04 first archive"
V2 = b"PK\x03\x04 second archive, different bytes"


async def _override_get_db():
    async with TestSessionLocal() as session:
        yield session


def _override_admin():
    return {"sub": "00000000-0000-0000-0000-000000000001", "username": "admin", "role": "admin"}


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
def local_root(tmp_path):
    storage._backend = "local"
    storage._local_path = tmp_path
    yield tmp_path
    storage._backend = "none"
    storage._client = None
    storage._local_path = None


@pytest.fixture
async def client(local_root):
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user_payload] = _override_admin
    # A storage failure surfaces as the 500 a real client sees, not as a raise.
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


def _files_under(root) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


async def _row_count() -> int:
    async with TestSessionLocal() as db:
        return (await db.execute(select(func.count()).select_from(DriverPackage))).scalar()


async def _create(client, filename: str, data: bytes):
    return await client.post(
        "/drivers",
        data={"name": "Consistency", "connection_type": "Management"},
        files={"file": (filename, io.BytesIO(data), "application/zip")},
    )


async def _replace(client, driver_id: str, filename: str, data: bytes):
    return await client.put(
        f"/drivers/{driver_id}/file",
        files={"file": (filename, io.BytesIO(data), "application/zip")},
    )


def _failing_upload(key, data, content_type=""):
    raise OSError(28, "No space left on device")


async def test_a_same_name_replace_keeps_the_archive_it_just_stored(client, local_root):
    driver_id = (await _create(client, "pkg.zip", V1)).json()["id"]

    resp = await _replace(client, driver_id, "pkg.zip", V2)

    assert resp.status_code == 200, resp.text
    assert resp.json()["sha256"] == hashlib.sha256(V2).hexdigest()
    assert _files_under(local_root) == [f"{driver_id}/pkg.zip"]
    download = await client.get(f"/drivers/{driver_id}/download")
    assert download.status_code == 200
    assert download.content == V2


async def test_a_storage_failure_on_create_commits_no_row(client, local_root):
    with patch.object(driver_service, "upload_object", _failing_upload):
        resp = await _create(client, "pkg.zip", V1)

    assert resp.status_code == 500
    # The framework's generic answer: no storage error text reaches the client.
    assert resp.text == "Internal Server Error"
    assert await _row_count() == 0
    assert _files_under(local_root) == []


async def test_a_storage_failure_on_replace_leaves_the_row_on_the_old_archive(client, local_root):
    created = (await _create(client, "old.zip", V1)).json()
    driver_id = created["id"]

    with patch.object(driver_service, "upload_object", _failing_upload):
        resp = await _replace(client, driver_id, "new.zip", V2)

    assert resp.status_code == 500
    assert resp.text == "Internal Server Error"
    row = (await client.get(f"/drivers/{driver_id}")).json()
    assert row["filename"] == "old.zip"
    assert row["sha256"] == hashlib.sha256(V1).hexdigest()
    assert row["size_bytes"] == len(V1)
    assert _files_under(local_root) == [f"{driver_id}/old.zip"]
    download = await client.get(f"/drivers/{driver_id}/download")
    assert download.status_code == 200
    assert download.content == V1
