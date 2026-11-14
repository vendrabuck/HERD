"""Driver package storage, hashing, and archive parsing run off the event loop (issue #1327).

Inventory is one uvicorn worker. Before #1327 the driver routes called the blocking
``app.storage`` functions (a MinIO round trip, or a local file read or write of up to
DRIVER_MAX_SIZE_BYTES), hashed the upload, and opened its zip or tar on the event
loop, so one driver upload or download stalled every other request on the replica.

Each test below wraps every blocking callable on the driver path with a recorder that
notes whether it was called with a running event loop in its thread. A call made on
the loop sees one; a call made through ``asyncio.to_thread`` does not. The recorders
delegate to fakes (storage) or to the real code (hashing, parsing), so the routes
answer as before. No assertion here has a wall-clock bound: the responsiveness test
sequences on threading events, and its only timeout is the failure path's way out of a
deadlock that an on-loop upload would cause.
"""

import asyncio
import hashlib
import io
import json
import threading
import types
import uuid
import zipfile
from unittest.mock import patch

import pytest
from app.database import Base, get_db
from app.dependencies.auth import get_current_user_payload
from app.main import app
from app.models.template import DeviceTemplate
from app.services import driver_service
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

# Long enough that a passing run never reaches it; it only ends the deadlock an
# on-loop upload creates, so the failing run reports instead of hanging.
_DEADLOCK_ESCAPE_SECONDS = 10

_storage: dict[str, bytes] = {}


def _off_loop() -> bool:
    """True when the calling thread has no running event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return True
    return False


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    def wrap(self, name, fn):
        def recorded(*args, **kwargs):
            self.calls.append((name, _off_loop()))
            return fn(*args, **kwargs)

        return recorded

    def names(self) -> set[str]:
        return {name for name, _ in self.calls}

    def on_loop(self) -> list[str]:
        return [name for name, off in self.calls if not off]


def _fake_upload(key: str, data: bytes, content_type: str = "") -> None:
    _storage[key] = data


def _fake_download(key: str) -> bytes:
    return _storage[key]


def _fake_delete(key: str) -> None:
    _storage.pop(key, None)


def _driver_zip(meta: dict | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("driver.py", "class Driver:\n    pass\n")
        zf.writestr("driver_metadata.json", json.dumps(meta or {"supports_dry_run": True}))
    return buf.getvalue()


async def _override_get_db():
    async with TestSessionLocal() as session:
        yield session


def _override_admin():
    return {"sub": "00000000-0000-0000-0000-000000000001", "username": "admin", "role": "admin"}


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    _storage.clear()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
def recorder():
    rec = _Recorder()
    with (
        patch.object(driver_service, "upload_object", rec.wrap("upload", _fake_upload)),
        patch.object(driver_service, "download_object", rec.wrap("download", _fake_download)),
        patch.object(driver_service, "delete_object", rec.wrap("delete", _fake_delete)),
        patch.object(
            driver_service,
            "hashlib",
            types.SimpleNamespace(sha256=rec.wrap("sha256", hashlib.sha256)),
        ),
        patch.object(
            driver_service,
            "_parse_driver_metadata",
            rec.wrap("parse", driver_service._parse_driver_metadata),
        ),
    ):
        yield rec


@pytest.fixture
async def client():
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user_payload] = _override_admin
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def _upload(client, name="Offload", filename="d.zip", data=None):
    return await client.post(
        "/drivers",
        data={"name": name, "connection_type": "Management"},
        files={"file": (filename, io.BytesIO(data or _driver_zip()), "application/zip")},
    )


async def test_driver_upload_hashes_parses_and_stores_off_the_event_loop(client, recorder):
    data = _driver_zip({"supports_dry_run": True, "supports_vrf": True})
    resp = await _upload(client, data=data)

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["sha256"] == hashlib.sha256(data).hexdigest()
    assert body["supports_dry_run"] is True
    assert body["supports_vrf"] is True
    assert recorder.names() >= {"sha256", "parse", "upload"}
    assert recorder.on_loop() == []


async def test_a_refused_oversized_metadata_upload_still_parses_off_the_event_loop(
    client, recorder
):
    big = b"{" + b" " * driver_service.MAX_DRIVER_METADATA_BYTES + b"}"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("driver_metadata.json", big)
    resp = await _upload(client, data=buf.getvalue())

    # The refusal raised inside the worker thread reaches the client unchanged.
    assert resp.status_code == 422
    assert resp.json()["detail"] == driver_service.METADATA_TOO_LARGE_DETAIL
    assert "parse" in recorder.names()
    assert "upload" not in recorder.names()
    assert _storage == {}
    assert recorder.on_loop() == []


async def test_duplicate_name_cleanup_delete_runs_off_the_event_loop(client, recorder):
    assert (await _upload(client, name="Dup")).status_code == 201
    recorder.calls.clear()

    resp = await _upload(client, name="Dup")

    assert resp.status_code == 409
    assert resp.json()["detail"] == "Driver with name 'Dup' already exists"
    assert recorder.names() >= {"sha256", "parse", "upload", "delete"}
    assert recorder.on_loop() == []
    # Only the first upload's object is left: the refused one was deleted again.
    assert len(_storage) == 1


async def test_file_replace_parses_stores_and_deletes_off_the_event_loop(client, recorder):
    driver_id = (await _upload(client, filename="old.zip")).json()["id"]
    recorder.calls.clear()

    new_data = _driver_zip({"supports_vrf": True})
    resp = await client.put(
        f"/drivers/{driver_id}/file",
        files={"file": ("new.zip", io.BytesIO(new_data), "application/zip")},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["sha256"] == hashlib.sha256(new_data).hexdigest()
    assert resp.json()["supports_vrf"] is True
    assert recorder.names() >= {"sha256", "parse", "upload", "delete"}
    assert recorder.on_loop() == []
    assert list(_storage) == [f"{driver_id}/new.zip"]


async def test_both_download_routes_read_storage_off_the_event_loop(client, recorder):
    data = _driver_zip()
    driver_id = (await _upload(client, data=data)).json()["id"]
    recorder.calls.clear()

    public = await client.get(f"/drivers/{driver_id}/download")
    internal = await client.get(
        f"/drivers/{driver_id}/internal-download", headers={"X-Internal-Token": "test-token"}
    )

    assert public.status_code == 200
    assert internal.status_code == 200
    assert public.content == data
    assert internal.content == data
    assert [name for name, _ in recorder.calls] == ["download", "download"]
    assert recorder.on_loop() == []


async def test_driver_delete_removes_the_object_off_the_event_loop(client, recorder):
    driver_id = (await _upload(client)).json()["id"]
    recorder.calls.clear()

    resp = await client.delete(f"/drivers/{driver_id}")

    assert resp.status_code == 204
    assert [name for name, _ in recorder.calls] == ["delete"]
    assert recorder.on_loop() == []
    assert _storage == {}


async def test_a_driver_referenced_by_a_template_is_refused_before_any_storage_call(
    client, recorder
):
    driver_id = (await _upload(client)).json()["id"]
    async with TestSessionLocal() as db:
        db.add(
            DeviceTemplate(
                name="T",
                template_type="device",
                vendor="V",
                model="M",
                driver_id=uuid.UUID(driver_id),
                sections=[],
            )
        )
        await db.commit()
    recorder.calls.clear()

    resp = await client.delete(f"/drivers/{driver_id}")

    assert resp.status_code == 409
    assert recorder.calls == []


async def test_health_answers_while_a_driver_upload_is_blocked_in_storage(client):
    """The issue's acceptance: the loop serves /health while an upload is in flight.

    The fake upload blocks its thread until the test releases it, and the test
    releases it only after /health has answered. With the upload on the event loop
    the loop itself is blocked, /health can never be served, and the fake gives up
    after _DEADLOCK_ESCAPE_SECONDS with an error, so the upload request fails.
    """
    started = threading.Event()
    release = threading.Event()

    def blocking_upload(key: str, data: bytes, content_type: str = "") -> None:
        started.set()
        if not release.wait(_DEADLOCK_ESCAPE_SECONDS):
            raise RuntimeError("upload never released: it blocked the event loop")
        _storage[key] = data

    with (
        patch.object(driver_service, "upload_object", blocking_upload),
        patch.object(driver_service, "delete_object", _fake_delete),
    ):
        upload_task = asyncio.create_task(_upload(client, name="Slow"))
        try:
            # Waiting in a worker thread keeps this coroutine from blocking the loop.
            assert await asyncio.to_thread(started.wait, _DEADLOCK_ESCAPE_SECONDS)
            assert not upload_task.done()

            health = await client.get("/health")

            assert health.status_code == 200
            assert health.json() == {"status": "ok", "service": "inventory"}
            # /health answered while the upload was still held in storage.
            assert not release.is_set()
            assert not upload_task.done()
        finally:
            release.set()
        resp = await upload_task

    assert resp.status_code == 201, resp.text
    assert list(_storage) == [f"{resp.json()['id']}/d.zip"]
