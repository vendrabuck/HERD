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
  long Retry-After, a request gives up after exactly 1 + ``MINIO_RETRIES`` attempts;
- over plain HTTP a whole request body must be sent within the connect timeout,
  however steadily a scripted store (``_FakeStore``) reads it;
- against that store, each request of a download, delete, single-part upload, or
  multipart upload is tried as its method allows, in the sequence INV-DRV-23's
  figures count, and four multipart uploads at once never overflow the pool;
- a create, like a download, holds no database connection while it waits on storage.

The only wall-clock bounds are escapes for the failure path (a deadlock or an
honoured Retry-After), each far above what a passing run takes.
"""

import asyncio
import contextlib
import io
import logging
import socket
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
    assert (storage.MINIO_CONNECT_TIMEOUT_SECONDS, storage.MINIO_READ_TIMEOUT_SECONDS) == (15, 30)
    assert kw["timeout"].connect_timeout == 15
    assert kw["timeout"].read_timeout == 30
    retries = kw["retries"]
    assert retries.total == storage.MINIO_RETRIES == 2
    assert retries.backoff_factor == 0.2
    assert sorted(retries.status_forcelist) == [500, 502, 503, 504]
    assert retries.respect_retry_after_header is False
    assert storage.MINIO_PART_THREADS == 3
    assert kw["maxsize"] == storage.MINIO_POOL_CONNECTIONS == 16
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
        except BaseException as exc:  # noqa: BLE001 (the test inspects it)
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
    # Short timeouts stand in for the real 15 s and 30 s; the attempt count is real.
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


# A scripted object store ---------------------------------------------------------

_LOCATION_XML = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<LocationConstraint xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
    b"</LocationConstraint>"
)
_CREATED_XML = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<InitiateMultipartUploadResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
    b"<Bucket>herd-drivers</Bucket><Key>k</Key><UploadId>u1</UploadId>"
    b"</InitiateMultipartUploadResult>"
)
_COMPLETED_XML = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<CompleteMultipartUploadResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
    b"<Bucket>herd-drivers</Bucket><Key>k</Key><ETag>e</ETag>"
    b"</CompleteMultipartUploadResult>"
)


class _Request:
    """One request the store was sent: its kind, the body bytes it read, and when
    each read of the body returned."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.received = 0
        self.read_times: list[float] = [time.monotonic()]


class _FakeStore:
    """A minimal S3 endpoint on 127.0.0.1 for the real MinIO client. The bucket's
    location lookup and HEAD are answered; every other request is recorded as a
    _Request and answered as ``policy(kind)`` says: "ok", "hold-for-peers" (answered
    once ``release_at`` requests have been in flight at once, or at the escape),
    "never" (read the body, then hold the request unanswered), or "stall-body" (a
    GET's headers and one byte of a longer body, then nothing). ``peak_in_flight``
    counts the most requests it held at once. The kinds are get, put, put-part, create,
    complete, abort, and delete. ``read_rate`` (bytes per second) slows every body
    read, and ``rcvbuf`` sets the store's socket receive buffer."""

    def __init__(self, policy, *, read_rate: float | None = None, rcvbuf: int | None = None):
        self.requests: list[_Request] = []
        self.stop = threading.Event()
        self._lock = threading.Lock()
        self.in_flight = 0
        self.peak_in_flight = 0
        self.release_at = float("inf")
        store = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _kind(self) -> str:
                query = self.path.partition("?")[2]
                if self.command == "GET" and "location" in query:
                    return "location"
                if self.command == "HEAD":
                    return "head"
                if self.command == "POST":
                    return "create" if "uploads" in query else "complete"
                if self.command == "PUT" and "partNumber" in query:
                    return "put-part"
                if self.command == "DELETE" and "uploadId" in query:
                    return "abort"
                return self.command.lower()

            def _read_body(self, request: _Request) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                started = time.monotonic()
                while request.received < length and not store.stop.is_set():
                    data = self.rfile.read1(min(65536, length - request.received))
                    if not data:
                        return
                    request.received += len(data)
                    request.read_times.append(time.monotonic())
                    if read_rate:
                        ahead = request.received / read_rate - (time.monotonic() - started)
                        if ahead > 0:
                            time.sleep(ahead)

            def _answer(self, status: int, body: bytes = b"", headers=()) -> None:
                self.send_response(status)
                for name, value in headers:
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _hold(self) -> None:
                store.stop.wait(_ESCAPE_SECONDS)
                self.close_connection = True

            def handle_request(self) -> None:
                kind = self._kind()
                if kind == "location":
                    return self._answer(200, _LOCATION_XML)
                if kind == "head":
                    return self._answer(200)
                request = _Request(kind)
                with store._lock:
                    store.requests.append(request)
                    store.in_flight += 1
                    store.peak_in_flight = max(store.peak_in_flight, store.in_flight)
                try:
                    self._script(kind, request)
                finally:
                    with store._lock:
                        store.in_flight -= 1

            def _script(self, kind: str, request: _Request) -> None:
                action = policy(kind)
                self._read_body(request)
                if action == "hold-for-peers":
                    deadline = time.monotonic() + _ESCAPE_SECONDS
                    while store.peak_in_flight < store.release_at and time.monotonic() < deadline:
                        time.sleep(0.01)
                if action == "never":
                    return self._hold()
                if kind == "get" and action == "stall-body":
                    self.send_response(200)
                    self.send_header("Content-Length", "100")
                    self.end_headers()
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    return self._hold()
                if kind == "create":
                    return self._answer(200, _CREATED_XML)
                if kind == "complete":
                    return self._answer(200, _COMPLETED_XML)
                if kind == "get":
                    return self._answer(200, b"archive")
                if kind in ("abort", "delete"):
                    return self._answer(204)
                return self._answer(200, headers=[("ETag", '"e"')])

            do_GET = do_HEAD = do_PUT = do_POST = do_DELETE = handle_request

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            request_queue_size = 64

            def handle_error(self, request, client_address):
                pass  # a client that gave up resets its connection; expected here

            def server_bind(self):
                if rcvbuf:
                    self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
                super().server_bind()

        self._server = Server(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]

    def kinds(self) -> list[str]:
        with self._lock:
            return [r.kind for r in self.requests]

    def __enter__(self) -> "_FakeStore":
        threading.Thread(target=self._server.serve_forever, args=(0.05,), daemon=True).start()
        with (
            patch.object(storage.settings, "minio_endpoint", f"127.0.0.1:{self.port}"),
            patch.object(storage.settings, "minio_use_ssl", False),
        ):
            storage.init_storage()
        return self

    def __exit__(self, *exc) -> None:
        self.stop.set()
        self._server.shutdown()
        self._server.server_close()


def _timed(call) -> tuple[BaseException | None, float]:
    """call() on a daemon thread: the error it raised and how long it took, or
    (None, inf) when it never ended."""
    outcome: dict = {}

    def run():
        started = time.monotonic()
        try:
            call()
        except BaseException as exc:  # noqa: BLE001 (the test inspects it)
            outcome["error"] = exc
        outcome["elapsed"] = time.monotonic() - started

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(_ESCAPE_SECONDS)
    if worker.is_alive():
        return None, float("inf")
    return outcome.get("error"), outcome["elapsed"]


def test_over_plain_http_a_whole_body_send_must_end_within_the_connect_timeout(
    reset_storage, monkeypatch
):
    """urllib3 sends a request body in one sendall under the connect timeout, and over
    plain HTTP the socket's timeout bounds that whole call, not each wait for
    progress. Scaled: a 0.5 s connect timeout and a store that reads a 5 MiB part at
    512 KiB/s, pausing at most about 0.13 s between reads. The socket buffers on both
    sides hold well under the part, so the send needs seconds more than the timeout;
    each attempt is given up while the store is still reading, as a broken connection
    (not a read timeout, which is 5 s here), and the PUT is tried three times."""
    monkeypatch.setattr(storage, "MINIO_CONNECT_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(storage, "MINIO_READ_TIMEOUT_SECONDS", 5)
    part = 5 * 1024 * 1024
    with _FakeStore(lambda kind: "ok", read_rate=512 * 1024, rcvbuf=65536) as store:
        error, elapsed = _timed(lambda: storage.upload_object("pkg.zip", b"x" * part))
        assert elapsed < _ESCAPE_SECONDS, "the body send was never given up"
        assert isinstance(error, urllib3.exceptions.MaxRetryError)
        assert isinstance(error.reason, urllib3.exceptions.ProtocolError)
        # Each attempt ended on the send, before any wait for an answer could.
        assert elapsed < storage.MINIO_READ_TIMEOUT_SECONDS
        assert store.kinds() == ["put"] * (1 + storage.MINIO_RETRIES)
        for request in store.requests:
            assert 0 < request.received < part
            first_window = [t for t in request.read_times if t - request.read_times[0] <= 0.5]
            gaps = [b - a for a, b in zip(first_window, first_window[1:], strict=False)]
            assert max(gaps) < storage.MINIO_CONNECT_TIMEOUT_SECONDS


_TEN_MIB = 10 * 1024 * 1024  # DRIVER_MAX_SIZE_BYTES: two 5 MiB parts
_TRIES = 1 + storage.MINIO_RETRIES


@pytest.mark.parametrize(
    ("answered", "call", "expected", "serial_reads"),
    [
        pytest.param(
            (),
            lambda: storage.download_object("pkg.zip"),
            ["get"] * _TRIES,
            _TRIES,
            id="a download the store never answers",
        ),
        pytest.param(
            (),
            lambda: storage.delete_object("pkg.zip"),
            ["delete"] * _TRIES,
            _TRIES,
            id="a delete the store never answers",
        ),
        pytest.param(
            (),
            lambda: storage.upload_object("pkg.zip", b"x" * 1024),
            ["put"] * _TRIES,
            _TRIES,
            id="a single-part upload the store never answers",
        ),
        pytest.param(
            ("stall-body",),
            lambda: storage.download_object("pkg.zip"),
            ["get"],
            1,
            id="a download whose answer stops after its headers",
        ),
        pytest.param(
            (),
            lambda: storage.upload_object("pkg.zip", b"x" * _TEN_MIB),
            ["create"],
            1,
            id="a multipart upload whose start goes unanswered",
        ),
        pytest.param(
            ("create",),
            lambda: storage.upload_object("pkg.zip", b"x" * _TEN_MIB),
            ["create"] + ["put-part"] * (2 * _TRIES) + ["abort"] * _TRIES,
            2 * _TRIES,
            id="a multipart upload whose parts go unanswered",
        ),
        pytest.param(
            ("create", "put-part"),
            lambda: storage.upload_object("pkg.zip", b"x" * _TEN_MIB),
            ["create", "put-part", "put-part", "complete"] + ["abort"] * _TRIES,
            1 + _TRIES,
            id="a multipart upload whose completion goes unanswered",
        ),
    ],
)
def test_each_request_of_a_storage_call_is_tried_as_its_method_allows(
    reset_storage, monkeypatch, answered, call, expected, serial_reads
):
    """The request sequences behind INV-DRV-23's figures, against a store that accepts
    connections and answers only the kinds listed. A GET, PUT, or DELETE is tried
    three times; a body read after the headers and a POST (a multipart upload's start
    or completion) are tried once; a multipart upload sends its two parts in parallel
    and, after a failure once started, its abort, tried three times. Scaled: a 0.2 s
    read timeout; the call takes at least one read timeout per request attempt waited
    in series (``serial_reads``), the figures' arithmetic."""
    monkeypatch.setattr(storage, "MINIO_CONNECT_TIMEOUT_SECONDS", 2)
    monkeypatch.setattr(storage, "MINIO_READ_TIMEOUT_SECONDS", 0.2)

    def policy(kind):
        if "stall-body" in answered:
            return "stall-body"
        return "ok" if kind in answered else "never"

    with _FakeStore(policy) as store:
        error, elapsed = _timed(call)
        assert elapsed < _ESCAPE_SECONDS, "a storage call outlived its bounds"
        assert error is not None
        assert store.kinds() == expected
    assert elapsed >= serial_reads * storage.MINIO_READ_TIMEOUT_SECONDS


async def test_concurrent_multipart_uploads_never_overflow_the_connection_pool(
    reset_storage, caplog
):
    """Four multipart uploads at once, one per storage thread, each sending its two
    parts in parallel: eight part requests in flight, past the four connections the
    pool used to keep. The pool keeps MINIO_POOL_CONNECTIONS, so urllib3 never
    discards a returned connection with a "Connection pool is full" warning."""
    caplog.set_level(logging.WARNING, logger="urllib3")
    parts = 2 * storage.DRIVER_STORAGE_THREADS
    with _FakeStore(lambda kind: "hold-for-peers" if kind == "put-part" else "ok") as store:
        store.release_at = parts
        await asyncio.wait_for(
            asyncio.gather(
                *(
                    storage.run_on_storage_thread(
                        storage.upload_object, f"pkg{n}.zip", b"x" * _TEN_MIB
                    )
                    for n in range(storage.DRIVER_STORAGE_THREADS)
                )
            ),
            _ESCAPE_SECONDS,
        )
        kinds = store.kinds()
        peak = store.peak_in_flight
    assert kinds.count("put-part") == parts
    assert kinds.count("complete") == storage.DRIVER_STORAGE_THREADS
    assert peak == parts
    assert "Connection pool is full" not in caplog.text


async def test_a_create_waiting_on_storage_holds_no_database_connection(client, file_db):
    """A driver create runs no query before its upload, so while the upload waits on
    storage its session holds no pooled connection (INV-DRV-22)."""
    engine, _ = file_db
    gate = _Gate()
    with patch.object(
        driver_service, "upload_object", lambda key, data, content_type="": gate(None)
    ):
        create = asyncio.ensure_future(
            client.post(
                "/drivers",
                data={"name": "Waiting", "connection_type": "Management"},
                files={"file": ("d.zip", io.BytesIO(_driver_zip(1)), "application/zip")},
            )
        )
        try:
            await _until(lambda: gate.entered == 1)
            assert engine.pool.checkedout() == 0
        finally:
            gate.release.set()
        resp = await create
    assert resp.status_code == 201, resp.text
