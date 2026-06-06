"""Security-edge coverage for driver package extraction.

Path traversal, absolute-path entries, symlink escapes in tar, corrupted
archives, and validation failures when driver.py crashes at import time.
"""

import io
import tarfile
import tempfile
import zipfile
from pathlib import Path

import pytest
from app.services.driver_loader import extract_driver_package, validate_driver

VALID_L1_DRIVER = """
class Driver:
    def __init__(self, context):
        self.context = context

    def login(self):
        return {"success": True}

    def logout(self):
        return {"success": True}

    def connect_ports(self, port_a, port_b):
        return {"success": True}

    def disconnect_ports(self, port_a, port_b):
        return {"success": True}

    def status(self):
        return {"reachable": True}
"""


def _zip_with_entries(entries: dict[str, bytes | str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries.items():
            if isinstance(content, str):
                content = content.encode()
            zf.writestr(name, content)
    return buf.getvalue()


def _tar_gz_with_entries(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in entries.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def test_zip_traversal_does_not_escape_destination():
    """Entries with `../` must not create files outside dest_dir."""
    payload = _zip_with_entries(
        {
            "driver.py": VALID_L1_DRIVER,
            "../escape.txt": b"owned",
        }
    )
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        dest = root / "driver"
        extract_driver_package(payload, "pkg.zip", dest)
        # Nothing may land outside the dest_dir, regardless of how zipfile
        # chose to normalize the traversal.
        assert not (root / "escape.txt").exists()
        for sibling in root.iterdir():
            assert sibling.resolve().is_relative_to(dest.resolve()) or sibling == dest


def test_zip_absolute_path_entry_is_contained():
    """A zip entry with an absolute path must be contained under dest_dir."""
    payload = _zip_with_entries(
        {
            "driver.py": VALID_L1_DRIVER,
            "/abs/path/file.txt": b"x",
        }
    )
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "driver"
        extract_driver_package(payload, "pkg.zip", dest)
        assert not Path("/abs/path/file.txt").exists()


def test_zip_corrupt_archive_raises():
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "driver"
        with pytest.raises(zipfile.BadZipFile):
            extract_driver_package(b"not-a-real-zip", "pkg.zip", dest)


def test_tar_gz_corrupt_archive_raises():
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "driver"
        with pytest.raises(tarfile.ReadError):
            extract_driver_package(b"not-a-real-tar", "pkg.tar.gz", dest)


def test_tar_gz_symlink_escape_is_blocked_by_data_filter():
    """Tarfile's filter='data' refuses symlinks that escape the destination."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        # Inner driver.py for completeness
        info = tarfile.TarInfo(name="driver.py")
        body = VALID_L1_DRIVER.encode()
        info.size = len(body)
        tf.addfile(info, io.BytesIO(body))
        # Malicious symlink pointing outside
        evil = tarfile.TarInfo(name="escape")
        evil.type = tarfile.SYMTYPE
        evil.linkname = "../../../etc/passwd"
        tf.addfile(evil)

    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "driver"
        with pytest.raises((tarfile.LinkOutsideDestinationError, tarfile.AbsoluteLinkError)):
            extract_driver_package(buf.getvalue(), "pkg.tar.gz", dest)


def test_tar_gz_traversal_blocked_by_data_filter():
    """Tarfile's filter='data' refuses entries containing .. path components."""
    payload = _tar_gz_with_entries(
        {
            "driver.py": VALID_L1_DRIVER.encode(),
            "../escape.txt": b"owned",
        }
    )
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "driver"
        with pytest.raises(tarfile.OutsideDestinationError):
            extract_driver_package(payload, "pkg.tar.gz", dest)


def test_validate_reports_driver_import_error():
    """A driver.py that crashes on import yields a load-failure validation error."""
    payload = _zip_with_entries({"driver.py": "raise RuntimeError('boom at import')"})
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "driver"
        extract_driver_package(payload, "pkg.zip", dest)
        errors = validate_driver(dest, "Layer 1 Switch")
        assert len(errors) == 1
        assert "Failed to load driver.py" in errors[0]


def test_validate_reports_syntax_error_in_driver():
    """A driver.py with a syntax error yields a load-failure validation error."""
    payload = _zip_with_entries({"driver.py": "def broken(:\n    pass"})
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "driver"
        extract_driver_package(payload, "pkg.zip", dest)
        errors = validate_driver(dest, "Layer 1 Switch")
        assert any("Failed to load driver.py" in e for e in errors)


def test_validate_rejects_driver_without_class():
    payload = _zip_with_entries({"driver.py": "X = 1  # no Driver class here"})
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "driver"
        extract_driver_package(payload, "pkg.zip", dest)
        errors = validate_driver(dest, "Layer 1 Switch")
        assert errors == ["driver.py must define a class named Driver"]
