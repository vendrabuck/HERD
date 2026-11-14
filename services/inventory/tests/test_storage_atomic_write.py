"""A local driver-storage write replaces the object atomically (issue #1327 review).

Since #1327 storage reads and writes run in worker threads, so they can overlap. A
file replace that keeps its file name reuses the storage key, and a write of that
key in place (``Path.write_bytes``: truncate, then write) let a concurrent download
return a truncated or mixed archive with a 200. The invariant pinned here: a reader
concurrent with any number of writers of the same key always gets exactly one
complete version, never a mix, and a failed write leaves the previous object and no
temporary file behind.

These tests use the real ``app.storage`` local backend on a temporary directory. The
thread test has bounded iterations and no wall-clock assertion; the open-handle test
is deterministic and needs no threads.
"""

import os
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
from app import storage
from app.services.driver_service import _validate_filename
from fastapi import HTTPException

KEY = "00000000-0000-0000-0000-000000000001/pkg.zip"
SIZE = 1024 * 1024
V1 = b"\x01" * SIZE
V2 = b"\x02" * SIZE


@pytest.fixture
def local_root(tmp_path):
    storage._backend = "local"
    storage._local_path = tmp_path
    yield tmp_path
    storage._backend = "none"
    storage._client = None
    storage._local_path = None


def _files_under(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def test_concurrent_readers_of_one_key_always_get_one_complete_version(local_root):
    storage.upload_object(KEY, V1)
    writes_per_writer = 60
    reads_per_reader = 120
    start = threading.Barrier(4)
    bad: list[str] = []
    errors: list[BaseException] = []

    def writer(first: bytes, second: bytes) -> None:
        try:
            start.wait()
            for i in range(writes_per_writer):
                storage.upload_object(KEY, first if i % 2 == 0 else second)
        except BaseException as exc:
            errors.append(exc)

    def reader() -> None:
        try:
            start.wait()
            for _ in range(reads_per_reader):
                got = storage.download_object(KEY)
                if got != V1 and got != V2:
                    bad.append(f"len={len(got)} distinct={sorted(set(got))[:4]}")
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=writer, args=(V1, V2)),
        threading.Thread(target=writer, args=(V2, V1)),
        threading.Thread(target=reader),
        threading.Thread(target=reader),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert bad == [], f"{len(bad)} torn reads, first: {bad[0]}"
    assert storage.download_object(KEY) in (V1, V2)
    assert _files_under(local_root) == [KEY]


def test_a_reader_holding_the_file_open_keeps_the_version_it_opened(local_root):
    storage.upload_object(KEY, V1)
    with open(local_root / KEY, "rb") as held:
        storage.upload_object(KEY, V2)
        # An in-place write would have truncated and rewritten the file this
        # handle reads; a rename onto the key leaves the opened version whole.
        assert held.read() == V1
    assert storage.download_object(KEY) == V2


class _FailingWriteFile:
    """Wraps the real temp file: writes a few bytes, then fails like a full disk."""

    def __init__(self, real):
        self._real = real

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self._real.close()
        return False

    def write(self, data):
        self._real.write(data[:10])
        raise OSError(28, "No space left on device")


def _fail_during_write():
    real_fdopen = os.fdopen
    return patch(
        "app.storage.os.fdopen",
        side_effect=lambda fd, *a, **k: _FailingWriteFile(real_fdopen(fd, *a, **k)),
    )


def _fail_at_rename():
    return patch("app.storage.os.replace", side_effect=OSError(18, "Invalid cross-device link"))


@pytest.mark.parametrize("failure", [_fail_during_write, _fail_at_rename])
def test_a_failed_write_leaves_the_old_object_and_no_temporary_file(local_root, failure):
    storage.upload_object(KEY, V1)

    with failure(), pytest.raises(OSError):
        storage.upload_object(KEY, V2)

    assert storage.download_object(KEY) == V1
    assert _files_under(local_root) == [KEY]


@pytest.mark.parametrize("failure", [_fail_during_write, _fail_at_rename])
def test_a_failed_first_write_leaves_nothing_behind(local_root, failure):
    with failure(), pytest.raises(OSError):
        storage.upload_object(KEY, V1)

    with pytest.raises(FileNotFoundError):
        storage.download_object(KEY)
    assert _files_under(local_root) == []


def test_the_temporary_name_can_never_be_a_driver_storage_key(local_root):
    real_replace = os.replace
    seen: list[Path] = []

    def recording_replace(src, dst):
        seen.append(Path(src))
        return real_replace(src, dst)

    with patch("app.storage.os.replace", side_effect=recording_replace):
        storage.upload_object(KEY, V1)

    (tmp,) = seen
    assert tmp.parent == (local_root / KEY).parent
    assert tmp.name.startswith(".pkg.zip.") and tmp.name.endswith(".tmp")
    # The upload's file-name check refuses it, so no package can be stored under it.
    with pytest.raises(HTTPException):
        _validate_filename(tmp.name)
