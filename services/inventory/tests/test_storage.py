"""Unit tests for storage.py (local filesystem backend)."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from app import storage


@pytest.fixture(autouse=True)
def reset_storage():
    """Reset module-level storage state between tests."""
    storage._backend = "none"
    storage._client = None
    storage._local_path = None
    yield
    storage._backend = "none"
    storage._client = None
    storage._local_path = None


# --- init_storage ---


def test_init_storage_local_backend():
    with tempfile.TemporaryDirectory() as tmpdir:
        with patch.object(storage.settings, "minio_endpoint", ""):
            with patch.object(storage.settings, "driver_storage_path", tmpdir):
                storage.init_storage()
                assert storage._backend == "local"
                assert storage._local_path == Path(tmpdir)


def _mock_minio_settings():
    """Context manager stack for minio settings."""
    import contextlib

    return contextlib.ExitStack()


def _patch_minio_init(mock_client):
    """Patch minio.Minio so the import inside init_storage gets our mock."""
    mock_minio_module = MagicMock()
    mock_minio_module.Minio.return_value = mock_client
    return patch.dict("sys.modules", {"minio": mock_minio_module})


def test_init_storage_minio_backend():
    mock_client = MagicMock()
    mock_client.bucket_exists.return_value = False
    with _patch_minio_init(mock_client):
        with patch.object(storage.settings, "minio_endpoint", "minio:9000"):
            with patch.object(storage.settings, "minio_bucket", "drivers"):
                storage.init_storage()
                assert storage._backend == "minio"
                mock_client.bucket_exists.assert_called_once_with("drivers")
                mock_client.make_bucket.assert_called_once_with("drivers")


def test_init_storage_minio_bucket_exists():
    mock_client = MagicMock()
    mock_client.bucket_exists.return_value = True
    with _patch_minio_init(mock_client):
        with patch.object(storage.settings, "minio_endpoint", "minio:9000"):
            with patch.object(storage.settings, "minio_bucket", "drivers"):
                storage.init_storage()
                mock_client.make_bucket.assert_not_called()


# --- Local backend operations ---


def test_upload_and_download_local():
    with tempfile.TemporaryDirectory() as tmpdir:
        storage._backend = "local"
        storage._local_path = Path(tmpdir)
        storage.upload_object("test/file.bin", b"hello world")
        data = storage.download_object("test/file.bin")
        assert data == b"hello world"


def test_download_local_not_found():
    with tempfile.TemporaryDirectory() as tmpdir:
        storage._backend = "local"
        storage._local_path = Path(tmpdir)
        with pytest.raises(FileNotFoundError):
            storage.download_object("nonexistent/file.bin")


def test_delete_local():
    with tempfile.TemporaryDirectory() as tmpdir:
        storage._backend = "local"
        storage._local_path = Path(tmpdir)
        storage.upload_object("test/file.bin", b"data")
        storage.delete_object("test/file.bin")
        with pytest.raises(FileNotFoundError):
            storage.download_object("test/file.bin")


def test_delete_local_missing_no_error():
    with tempfile.TemporaryDirectory() as tmpdir:
        storage._backend = "local"
        storage._local_path = Path(tmpdir)
        # Should not raise
        storage.delete_object("nonexistent/file.bin")


# --- Uninitialized backend ---


def test_upload_without_init_raises():
    with pytest.raises(RuntimeError, match="Storage not initialized"):
        storage.upload_object("key", b"data")


def test_download_without_init_raises():
    with pytest.raises(RuntimeError, match="Storage not initialized"):
        storage.download_object("key")


def test_delete_without_init_raises():
    with pytest.raises(RuntimeError, match="Storage not initialized"):
        storage.delete_object("key")


# --- MinIO backend operations (mocked) ---


def test_upload_minio():
    mock_client = MagicMock()
    storage._backend = "minio"
    storage._client = mock_client
    with patch.object(storage.settings, "minio_bucket", "bucket"):
        storage.upload_object("key/file.zip", b"data")
        mock_client.put_object.assert_called_once()


def test_download_minio():
    mock_response = MagicMock()
    mock_response.read.return_value = b"minio-data"
    mock_client = MagicMock()
    mock_client.get_object.return_value = mock_response
    storage._backend = "minio"
    storage._client = mock_client
    with patch.object(storage.settings, "minio_bucket", "bucket"):
        result = storage.download_object("key/file.zip")
        assert result == b"minio-data"
        mock_response.close.assert_called_once()
        mock_response.release_conn.assert_called_once()


def test_delete_minio():
    mock_client = MagicMock()
    storage._backend = "minio"
    storage._client = mock_client
    with patch.object(storage.settings, "minio_bucket", "bucket"):
        storage.delete_object("key/file.zip")
        mock_client.remove_object.assert_called_once_with("bucket", "key/file.zip")
