import io
import logging
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

_backend: str = "none"  # "minio" or "local"
_client = None  # Minio instance when using minio backend
_local_path: Path | None = None


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
        dest = _local_path / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
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
        dest = _local_path / key
        if not dest.exists():
            raise FileNotFoundError(f"Driver file not found: {key}")
        return dest.read_bytes()
    else:
        raise RuntimeError("Storage not initialized; call init_storage() first")


def delete_object(key: str) -> None:
    if _backend == "minio":
        _client.remove_object(settings.minio_bucket, key)
    elif _backend == "local":
        dest = _local_path / key
        if dest.exists():
            dest.unlink()
    else:
        raise RuntimeError("Storage not initialized; call init_storage() first")
