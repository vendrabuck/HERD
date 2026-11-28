"""Driver storage runs on its own bounded threads; MinIO requests are bounded (issue #1416).

Before #1416 the storage calls ran through ``asyncio.to_thread``, on asyncio's default
executor, the one ``loop.getaddrinfo`` resolves names on, and the MinIO client kept
its library defaults (a 5-minute connect and read timeout, 5 retries). A stalled
object store could then fill that executor with storage calls for minutes and stall
every new database and NATS connection. These tests pin INV-DRV-22 and INV-DRV-23:

- storage calls run on ``app.storage``'s own pool of ``DRIVER_STORAGE_THREADS`` (4)
  threads: with more calls blocked than the default executor has threads, at most
  four run at once and ``loop.getaddrinfo`` still answers;
- through the routes, concurrent downloads blocked in storage hold no pooled
  database connection, and each answers its own archive once released;
- the MinIO client sends through a pool built with the named timeouts and retry
  count, and against a real socket that never answers, or that answers 503 with a
  long Retry-After, a request gives up after exactly 1 + ``MINIO_RETRIES`` attempts.

The only wall-clock bounds are escapes for the failure path (a deadlock or an
honoured Retry-After), each far above what a passing run takes.
"""

import asyncio
import contextlib
import io
import socket
import threading
import time
import zipfile
from unittest.mock import MagicMock, patch

import pytest
import urllib3
from app import storage
from app.database import Base, get_db
from app.dependencies.auth import get_current_user_payload
from app.main import app
from app.services import driver_service
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# Far above a passing run; only ends a wait that a regression turns into a deadlock.
_ESCAPE_SECONDS = 20
# More calls than asyncio's default executor ever has threads (min(32, cpus + 4)).
_MORE_THAN_THE_DEFAULT_EXECUTOR = 40


class _Gate:
    """A storage call that blocks until released, recording how many run at once
    and on which threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.release = threading.Event()
        self.running = 0
        self.entered = 0
        self.peak = 0
        self.thread_names: set[str] = set()

    def __call__(self, value):
        with self._lock:
            self.running += 1
            self.entered += 1
            self.peak = max(self.peak, self.running)
            self.thread_names.add(threading.current_thread().name)
        try:
            if not self.release.wait(_ESCAPE_SECONDS):
                raise RuntimeError("storage call never released")
            return value
        finally:
            with self._lock:
                self.running -= 1


async def _until(predicate) -> None:
    """Wait on the loop (never on a worker thread) until predicate() holds."""
    deadline = time.monotonic() + _ESCAPE_SECONDS
    while not predicate():
        assert time.monotonic() < deadline, "the expected storage calls never started"
        await asyncio.sleep(0.01)


def test_the_storage_pool_is_its_own_and_sized_by_the_named_constant():
    assert storage.DRIVER_STORAGE_THREADS == 4
    pool = storage.storage_executor()
    assert pool is storage.storage_executor()
    assert pool._max_workers == storage.DRIVER_STORAGE_THREADS


async def test_storage_calls_run_at_most_the_bound_at_once_and_leave_name_resolution_free():
    gate = _Gate()
    calls = [
        asyncio.ensure_future(storage.run_on_storage_thread(gate, i))
        for i in range(_MORE_THAN_THE_DEFAULT_EXECUTOR)
    ]
    try:
        await _until(lambda: gate.entered >= storage.DRIVER_STORAGE_THREADS)

        # Name resolution runs on asyncio's default executor; with the storage calls
        # on that executor it would wait behind them until the escape.
        loop = asyncio.get_running_loop()
        resolved = await asyncio.wait_for(
            loop.getaddrinfo("127.0.0.1", 80, type=socket.SOCK_STREAM), _ESCAPE_SECONDS
        )
        assert resolved

        assert gate.entered == storage.DRIVER_STORAGE_THREADS
        assert gate.running == storage.DRIVER_STORAGE_THREADS
    finally:
        gate.release.set()
    assert await asyncio.gather(*calls) == list(range(_MORE_THAN_THE_DEFAULT_EXECUTOR))
    assert gate.peak == storage.DRIVER_STORAGE_THREADS
    assert gate.thread_names and all(n.startswith("driver-storage") for n in gate.thread_names)


async def test_an_error_raised_on_a_storage_thread_reaches_the_caller_unchanged():
    def refuse():
        raise FileNotFoundError("gone")

    with pytest.raises(FileNotFoundError, match="^gone$"):
        await storage.run_on_storage_thread(refuse)


# Through the routes --------------------------------------------------------------


def _driver_zip(n: int) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("driver.py", f"class Driver:\n    N = {n}\n")
    return buf.getvalue()


@pytest.fixture
async def file_db(tmp_path):
    """A file-backed SQLite behind a real connection pool, so the test can count
    the connections checked out while downloads wait on storage."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'inv.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine, async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def client(file_db):
    _, sessions = file_db

    async def override_get_db():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_payload] = lambda: {
        "sub": "00000000-0000-0000-0000-000000000001",
        "username": "admin",
        "role": "admin",
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def test_downloads_waiting_on_storage_hold_no_database_connection_and_respect_the_bound(
    client, file_db
):
    engine, _ = file_db
    stored: dict[str, bytes] = {}

    def fake_upload(key: str, data: bytes, content_type: str = "") -> None:
        stored[key] = data

    gate = _Gate()

    def blocked_download(key: str) -> bytes:
        return gate(stored[key])

    with (
        patch.object(driver_service, "upload_object", fake_upload),
        patch.object(driver_service, "download_object", blocked_download),
    ):
        ids = {}
        for n in range(6):
            resp = await client.post(
                "/drivers",
                data={"name": f"Pkg{n}", "connection_type": "Management"},
                files={"file": ("d.zip", io.BytesIO(_driver_zip(n)), "application/zip")},
            )
            assert resp.status_code == 201, resp.text
            ids[resp.json()["id"]] = _driver_zip(n)

        downloads = {
            driver_id: asyncio.ensure_future(client.get(f"/drivers/{driver_id}/download"))
            for driver_id in ids
        }
        try:
            await _until(lambda: gate.entered >= storage.DRIVER_STORAGE_THREADS)
            # Four are in storage, two wait for a thread; none holds a connection.
            await _until(lambda: engine.pool.checkedout() == 0)
            health = await client.get("/health")
            assert health.status_code == 200
            assert gate.entered == storage.DRIVER_STORAGE_THREADS
            assert engine.pool.checkedout() == 0
        finally:
            gate.release.set()
        for driver_id, task in downloads.items():
            resp = await task
            assert resp.status_code == 200
            assert resp.content == ids[driver_id]
    assert gate.peak == storage.DRIVER_STORAGE_THREADS


# The MinIO client ----------------------------------------------------------------


@pytest.fixture
def reset_storage():
    yield
    storage._backend = "none"
    storage._client = None
    storage._local_path = None


def test_the_minio_client_sends_through_a_pool_with_the_named_bounds(reset_storage):
    minio_module = MagicMock()
    minio_module.Minio.return_value.bucket_exists.return_value = True
    with (
        patch.dict("sys.modules", {"minio": minio_module}),
        patch.object(storage.settings, "minio_endpoint", "minio:9000"),
    ):
        storage.init_storage()

    pool = minio_module.Minio.call_args.kwargs["http_client"]
    assert isinstance(pool, urllib3.PoolManager)
    kw = pool.connection_pool_kw
    assert (storage.MINIO_CONNECT_TIMEOUT_SECONDS, storage.MINIO_READ_TIMEOUT_SECONDS) == (5, 30)
    assert kw["timeout"].connect_timeout == 5
    assert kw["timeout"].read_timeout == 30
    retries = kw["retries"]
    assert retries.total == storage.MINIO_RETRIES == 2
    assert retries.backoff_factor == 0.2
    assert sorted(retries.status_forcelist) == [500, 502, 503, 504]
    assert retries.respect_retry_after_header is False
    assert kw["maxsize"] == storage.DRIVER_STORAGE_THREADS
    assert kw["cert_reqs"] == "CERT_REQUIRED"


@contextlib.contextmanager
def _server(handle):
    """A TCP server on 127.0.0.1 that hands each accepted connection to handle(conn)
    on its own daemon thread and counts the connections."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    accepted: list[socket.socket] = []
    stop = threading.Event()

    def accept_loop():
        listener.settimeout(0.1)
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            accepted.append(conn)
            threading.Thread(target=handle, args=(conn,), daemon=True).start()

    threading.Thread(target=accept_loop, daemon=True).start()
    try:
        yield listener.getsockname()[1], accepted
    finally:
        stop.set()
        listener.close()
        for conn in accepted:
            conn.close()


def _never_answer(conn: socket.socket) -> None:
    with contextlib.suppress(OSError):
        while conn.recv(65536):
            pass


_requests_answered_503: list[int] = []


def _answer_503_with_a_long_retry_after(conn: socket.socket) -> None:
    with contextlib.suppress(OSError):
        buf = b""
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                return
            buf += chunk
            while b"\r\n\r\n" in buf:
                _, buf = buf.split(b"\r\n\r\n", 1)
                _requests_answered_503.append(1)
                conn.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\nRetry-After: 3600\r\n"
                    b"Content-Length: 0\r\n\r\n"
                )


def _init_against(port: int) -> tuple[BaseException | None, float]:
    """init_storage against the server, on a daemon thread; the error it raised and
    how long it took, or (None, inf) when it never ended."""
    outcome: dict = {}

    def run():
        started = time.monotonic()
        try:
            storage.init_storage()
        except BaseException as exc:  # noqa: BLE001: the test inspects it
            outcome["error"] = exc
        outcome["elapsed"] = time.monotonic() - started

    worker = threading.Thread(target=run, daemon=True)
    with (
        patch.object(storage.settings, "minio_endpoint", f"127.0.0.1:{port}"),
        patch.object(storage.settings, "minio_use_ssl", False),
    ):
        worker.start()
        worker.join(_ESCAPE_SECONDS)
    if worker.is_alive():
        return None, float("inf")
    return outcome.get("error"), outcome["elapsed"]


def test_a_store_that_never_answers_is_given_up_after_the_retries(reset_storage, monkeypatch):
    # Short timeouts stand in for the real 5 s and 30 s; the attempt count is real.
    monkeypatch.setattr(storage, "MINIO_CONNECT_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(storage, "MINIO_READ_TIMEOUT_SECONDS", 0.3)
    with _server(_never_answer) as (port, accepted):
        error, elapsed = _init_against(port)
        assert elapsed < _ESCAPE_SECONDS, "a silent store held the call past the escape"
        assert error is not None
        assert len(accepted) == 1 + storage.MINIO_RETRIES
    assert storage._backend == "none"


def test_a_long_retry_after_is_not_waited_out(reset_storage):
    _requests_answered_503.clear()
    with _server(_answer_503_with_a_long_retry_after) as (port, _):
        error, elapsed = _init_against(port)
        assert elapsed < _ESCAPE_SECONDS, "the Retry-After header was honoured"
        assert error is not None
        assert len(_requests_answered_503) == 1 + storage.MINIO_RETRIES
    assert storage._backend == "none"
