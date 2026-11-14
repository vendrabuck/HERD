"""Driver package object storage: MinIO when MINIO_ENDPOINT is set, else a local directory.

Every function here is synchronous and blocking (a MinIO network round trip, or a
local file read, write, or unlink of up to DRIVER_MAX_SIZE_BYTES). ``driver_service``
is the only caller of ``upload_object``, ``download_object`` and ``delete_object``,
and runs each through ``asyncio.to_thread``, never on the event loop (issue #1327).
``init_storage`` is called once, from the lifespan in ``app/main.py``, before the
app serves a request. Because reads and writes of one key can now overlap, a local
write replaces the file atomically (``_write_local_atomically``).
"""

import io
import logging
import os
import secrets
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

_backend: str = "none"  # "minio" or "local"
_client = None  # Minio instance when using minio backend
_local_path: Path | None = None


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
