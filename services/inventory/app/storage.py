"""Driver package object storage: MinIO when MINIO_ENDPOINT is set, else a local directory.

Every storage function here is synchronous and blocking (a MinIO network round trip,
or a local file read, write, or unlink of up to DRIVER_MAX_SIZE_BYTES).
``driver_service`` is the only caller of ``upload_object``, ``download_object`` and
``delete_object``, and runs each through ``run_on_storage_thread``, never on the event
loop (issue #1327) and never on asyncio's default executor (issue #1416).
``init_storage`` is called once, from the lifespan in ``app/main.py``, before the app
serves a request. Because reads and writes of one key can overlap, a local write
replaces the file atomically (``_write_local_atomically``).

The storage threads (issue #1416). ``run_on_storage_thread`` runs a call on this
module's own pool of ``DRIVER_STORAGE_THREADS`` (4) threads. asyncio's default
executor is the one ``loop.getaddrinfo`` resolves names on, so new database and NATS
connections wait for a free thread there; with storage calls on their own pool, a
stalled object store can hold at most those four threads, and name resolution, the
event loop, and every other worker thread stay free. A fifth call waits in the pool's
queue for a thread, with no bound on the wait; a call cancelled while it waits never
runs, and one cancelled while it runs keeps its thread until it ends, so the bound
holds whatever is cancelled. At most four package archives are read or written at once.

The MinIO client's HTTP pool (``_minio_http_client``) replaces the library's defaults
(a 5-minute connect and read timeout and 5 retries). A connect gives up after
``MINIO_CONNECT_TIMEOUT_SECONDS`` (15), and the request is sent under the same
timeout: urllib3 hands a request body (one part, at most 5 MiB) to one ``sendall``,
so over plain HTTP (``MINIO_USE_SSL=false``, the compose default) the whole body must
be sent within 15 s, about 341 KiB/s for a 5 MiB part, however steadily the store
takes it; over TLS the timeout applies to each send. A wait for the next bytes of an
answer gives up after ``MINIO_READ_TIMEOUT_SECONDS`` (30). A GET, HEAD, PUT, or
DELETE that fails that way, or is answered 500, 502, 503, or 504, is tried again at
most ``MINIO_RETRIES`` (2) times, with backoffs of 0 s and then 0.4 s between the
attempts and never a wait the store names in a Retry-After header; urllib3 tries a
POST again only when its connection fails.

Those bound one request. A storage call is one request, except an upload over 5 MiB
(minio's MIN_PART_SIZE), which minio sends as a multipart upload: a POST to start it,
its 5 MiB parts as PUTs in parallel on minio's own threads, a POST to complete it,
and, after any failure once it has started, a DELETE to abort it. Against a store
that accepts connections and never answers:

- a download, a delete, or an upload of at most 5 MiB ends within three 30 s attempts
  and the 0.4 s backoff, 90.4 s;
- a download whose answer stops after its headers ends 30 s after its last bytes (a
  body read is not tried again);
- a multipart upload ends within 30 s when its start goes unanswered; within about
  181 s when the start is answered and the parts are not (the parts' 90.4 s, then the
  abort's 90.4 s); and about 120 s after its parts are stored when the completion goes
  unanswered (30 s, then the abort's 90.4 s).

Apart from a body send over plain HTTP, these are bounds on each wait for progress,
not on a whole transfer: a store that keeps sending a few bytes at a time is not cut
off by them, and the lookup of the store's host name (on the storage thread) is not
bounded by the connect timeout.
"""

import asyncio
import contextvars
import functools
import io
import logging
import os
import secrets
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TypeVar

from app.config import settings

logger = logging.getLogger(__name__)

# At most this many driver storage calls (and package inspections, which
# driver_service runs on the same threads) run at once in the process.
DRIVER_STORAGE_THREADS = 4

# The MinIO client's per-request bounds (issue #1416). Named constants rather than
# settings, by the issue's triage: the values bound a failure, they do not tune a
# deployment.
MINIO_CONNECT_TIMEOUT_SECONDS = 15
MINIO_READ_TIMEOUT_SECONDS = 30
MINIO_RETRIES = 2
MINIO_RETRY_BACKOFF_FACTOR = 0.2
MINIO_RETRY_STATUSES = (500, 502, 503, 504)

_T = TypeVar("_T")

_backend: str = "none"  # "minio" or "local"
_client = None  # Minio instance when using minio backend
_local_path: Path | None = None
_executor: ThreadPoolExecutor | None = None


def storage_executor() -> ThreadPoolExecutor:
    """The process-wide pool every ``run_on_storage_thread`` call runs on, built on
    first use. Each service runs one uvicorn process; the pool is not tied to a loop."""
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(
            max_workers=DRIVER_STORAGE_THREADS, thread_name_prefix="driver-storage"
        )
    return _executor


async def run_on_storage_thread(func: Callable[..., _T], *args: object) -> _T:
    """``func(*args)`` on a storage thread; it returns what ``func`` returns, or
    raises what it raised (an ``HTTPException`` included, unchanged). The caller's
    context variables are copied to the thread, as ``asyncio.to_thread`` does."""
    loop = asyncio.get_running_loop()
    call = functools.partial(contextvars.copy_context().run, func, *args)
    return await loop.run_in_executor(storage_executor(), call)


def _minio_http_client():
    """The urllib3 pool the MinIO client sends through: the library's own default
    pool with the bounds above in place of its 5-minute timeouts and 5 retries. The
    certificate checks are the library's defaults (``SSL_CERT_FILE`` or certifi's
    bundle, certificates required)."""
    import certifi
    import urllib3

    return urllib3.PoolManager(
        timeout=urllib3.Timeout(
            connect=MINIO_CONNECT_TIMEOUT_SECONDS, read=MINIO_READ_TIMEOUT_SECONDS
        ),
        maxsize=DRIVER_STORAGE_THREADS,
        cert_reqs="CERT_REQUIRED",
        ca_certs=os.environ.get("SSL_CERT_FILE") or certifi.where(),
        retries=urllib3.Retry(
            total=MINIO_RETRIES,
            backoff_factor=MINIO_RETRY_BACKOFF_FACTOR,
            status_forcelist=list(MINIO_RETRY_STATUSES),
            respect_retry_after_header=False,
        ),
    )


def _resolve_local(key: str) -> Path:
    """Resolve a storage key to an absolute path inside the local storage root.

    Guards against path traversal: the key is caller-influenced (driver storage
    keys are built from the uploaded filename), so a key like "../../etc/x" or an
    absolute path would otherwise let upload_object/download_object/delete_object
    write, read, or delete files outside the driver storage directory (CWE-22).
    We resolve the joined path and require it to stay within the resolved root,
    raising ValueError otherwise. This is the single chokepoint for every local
    filesystem operation, so the check cannot be bypassed by an unsanitized
    caller.
    """
    if _local_path is None:
        raise RuntimeError("Storage not initialized; call init_storage() first")
    root = _local_path.resolve()
    candidate = (root / key).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"Invalid storage key escapes storage root: {key!r}")
    return candidate


def _write_local_atomically(dest: Path, data: bytes) -> None:
    """Write ``data`` to ``dest`` so a reader sees the old file or the new one, whole.

    A file replace that keeps its file name reuses the storage key, and reads and
    writes run in worker threads (issue #1327), so writing ``dest`` in place let a
    concurrent download return a truncated or mixed archive. The bytes go to a new,
    uniquely named file in the same directory, which ``os.replace`` then renames
    onto ``dest`` (atomic on POSIX within one file system): a reader that opened
    ``dest`` before the rename keeps reading the old file, and one that opens it
    after reads the new one. Of two concurrent writers the later rename wins whole.

    The temporary name starts with a dot and ends in ``.tmp``, so it can never equal
    a driver storage key (a package file name must end in ``.zip`` or ``.tar.gz``),
    and ``O_EXCL`` refuses an existing file. It is opened with mode 0o666 so the
    umask applies as it did to ``Path.write_bytes``. On any failure the temporary
    file is removed and ``dest`` is left as it was.
    """
    tmp = dest.with_name(f".{dest.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def init_storage() -> None:
    global _backend, _client, _local_path
    if settings.minio_endpoint:
        from minio import Minio

        _client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_use_ssl,
            http_client=_minio_http_client(),
        )
        if not _client.bucket_exists(settings.minio_bucket):
            _client.make_bucket(settings.minio_bucket)
            logger.info("Created MinIO bucket: %s", settings.minio_bucket)
        else:
            logger.info("MinIO bucket exists: %s", settings.minio_bucket)
        _backend = "minio"
    else:
        _local_path = Path(settings.driver_storage_path)
        _local_path.mkdir(parents=True, exist_ok=True)
        _backend = "local"
        logger.info("Using local filesystem driver storage: %s", _local_path)


def upload_object(key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
    if _backend == "minio":
        _client.put_object(
            settings.minio_bucket,
            key,
            io.BytesIO(data),
            length=len(data),
            content_type=content_type,
        )
    elif _backend == "local":
        dest = _resolve_local(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        _write_local_atomically(dest, data)
    else:
        raise RuntimeError("Storage not initialized; call init_storage() first")


def download_object(key: str) -> bytes:
    if _backend == "minio":
        response = _client.get_object(settings.minio_bucket, key)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()
    elif _backend == "local":
        dest = _resolve_local(key)
        if not dest.exists():
            raise FileNotFoundError(f"Driver file not found: {key}")
        return dest.read_bytes()
    else:
        raise RuntimeError("Storage not initialized; call init_storage() first")


def delete_object(key: str) -> None:
    if _backend == "minio":
        _client.remove_object(settings.minio_bucket, key)
    elif _backend == "local":
        dest = _resolve_local(key)
        if dest.exists():
            dest.unlink()
    else:
        raise RuntimeError("Storage not initialized; call init_storage() first")
