"""Security-edge coverage for driver package extraction.

Path traversal, absolute-path entries, symlink escapes in tar, corrupted
archives, the extraction ceilings on entry count and declared uncompressed
size (issue #1115), and validation that never runs driver.py's own code
(issue #1114).
"""

import gzip
import io
import struct
import tarfile
import tempfile
import zipfile
from pathlib import Path

import pytest
from app.services import driver_loader
from app.services.driver_loader import (
    MAX_ARCHIVE_ENTRIES,
    MAX_EXTRACTED_BYTES,
    PackageLimitError,
    extract_driver_package,
    validate_driver,
)

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


def test_validate_never_runs_driver_top_level_code(tmp_path):
    """validate_driver parses driver.py and never imports it (issue #1114): a
    top-level side effect does not run, and a top-level raise is not an error,
    because only the sandbox ever executes package code."""
    flag = tmp_path / "ran.flag"
    driver = f'open(r"{flag}", "w").write("ran")\nraise RuntimeError("boom at import")\n'
    payload = _zip_with_entries({"driver.py": driver + VALID_L1_DRIVER})
    dest = tmp_path / "driver"
    extract_driver_package(payload, "pkg.zip", dest)
    assert validate_driver(dest, "Layer 1 Switch") == []
    assert not flag.exists()


def test_validate_reports_syntax_error_in_driver():
    """A driver.py with a syntax error yields a load-failure validation error
    naming the parser's exception class only."""
    payload = _zip_with_entries({"driver.py": "def broken(:\n    pass"})
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "driver"
        extract_driver_package(payload, "pkg.zip", dest)
        errors = validate_driver(dest, "Layer 1 Switch")
        assert errors == ["Failed to load driver.py: SyntaxError"]


def test_validate_rejects_driver_without_class():
    payload = _zip_with_entries({"driver.py": "X = 1  # no Driver class here"})
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "driver"
        extract_driver_package(payload, "pkg.zip", dest)
        errors = validate_driver(dest, "Layer 1 Switch")
        assert errors == ["driver.py must define a class named Driver"]


# --- extraction ceilings (issue #1115) ---------------------------------------
#
# Oversized archives are built cheaply: a forged declared size (a zip central
# directory field, a tar header with no data behind it) or many empty entries.
# The boundary tests lower the ceilings so an archive exactly at them can be
# extracted for real without writing 100 MiB.

ENTRIES_REFUSAL = (
    f"Driver package has more than {MAX_ARCHIVE_ENTRIES} entries; nothing was extracted"
)
BYTES_REFUSAL = (
    f"Driver package declares more than {MAX_EXTRACTED_BYTES} bytes uncompressed; "
    "nothing was extracted"
)


def test_extraction_ceilings_are_the_documented_constants():
    assert MAX_EXTRACTED_BYTES == 100 * 1024 * 1024
    assert MAX_ARCHIVE_ENTRIES == 10_000


def _zip_declaring_size(declared: int) -> bytes:
    """A one-entry zip whose central directory claims `declared` uncompressed bytes."""
    payload = _zip_with_entries({"driver.py": VALID_L1_DRIVER})
    central = payload.index(b"PK\x01\x02")
    # The central directory header keeps the uncompressed size at offset 24.
    return payload[: central + 24] + struct.pack("<I", declared) + payload[central + 28 :]


def _tar_gz_declaring_size(declared: int) -> bytes:
    """A tar.gz whose only member header claims `declared` bytes with no data behind it."""
    info = tarfile.TarInfo(name="driver.py")
    info.size = declared
    return gzip.compress(info.tobuf(format=tarfile.GNU_FORMAT) + b"\0" * 1024)


def _zip_with_empty_entries(count: int) -> bytes:
    return _zip_with_entries({f"f{i}": b"" for i in range(count)})


def _tar_gz_with_empty_entries(count: int) -> bytes:
    return _tar_gz_with_entries({f"f{i}": b"" for i in range(count)})


def test_zip_declaring_more_than_the_byte_ceiling_is_refused_before_writing(tmp_path):
    dest = tmp_path / "driver"
    with pytest.raises(PackageLimitError) as exc:
        extract_driver_package(_zip_declaring_size(MAX_EXTRACTED_BYTES + 1), "pkg.zip", dest)
    assert str(exc.value) == BYTES_REFUSAL
    assert not dest.exists()


def test_tar_gz_declaring_more_than_the_byte_ceiling_is_refused_before_writing(tmp_path):
    dest = tmp_path / "driver"
    with pytest.raises(PackageLimitError) as exc:
        extract_driver_package(_tar_gz_declaring_size(MAX_EXTRACTED_BYTES + 1), "pkg.tar.gz", dest)
    assert str(exc.value) == BYTES_REFUSAL
    assert not dest.exists()


def test_zip_with_more_than_the_entry_ceiling_is_refused_before_writing(tmp_path):
    dest = tmp_path / "driver"
    with pytest.raises(PackageLimitError) as exc:
        extract_driver_package(_zip_with_empty_entries(MAX_ARCHIVE_ENTRIES + 1), "pkg.zip", dest)
    assert str(exc.value) == ENTRIES_REFUSAL
    assert not dest.exists()


def test_tar_gz_with_more_than_the_entry_ceiling_is_refused_before_writing(tmp_path):
    dest = tmp_path / "driver"
    with pytest.raises(PackageLimitError) as exc:
        extract_driver_package(_tar_gz_with_empty_entries(MAX_ARCHIVE_ENTRIES + 1), "pkg.tgz", dest)
    assert str(exc.value) == ENTRIES_REFUSAL
    assert not dest.exists()


@pytest.mark.parametrize("filename", ["pkg.zip", "pkg.tar.gz"])
def test_archive_exactly_at_both_ceilings_extracts(tmp_path, monkeypatch, filename):
    """Two entries of 3 and 4 bytes: exactly at a 2-entry, 7-byte ceiling passes."""
    monkeypatch.setattr(driver_loader, "MAX_ARCHIVE_ENTRIES", 2)
    monkeypatch.setattr(driver_loader, "MAX_EXTRACTED_BYTES", 7)
    entries = {"a.txt": b"abc", "b.txt": b"defg"}
    payload = (
        _zip_with_entries(entries) if filename.endswith(".zip") else _tar_gz_with_entries(entries)
    )
    dest = tmp_path / "driver"
    extract_driver_package(payload, filename, dest)
    assert (dest / "a.txt").read_bytes() == b"abc"
    assert (dest / "b.txt").read_bytes() == b"defg"


@pytest.mark.parametrize("filename", ["pkg.zip", "pkg.tar.gz"])
def test_archive_one_byte_over_the_ceiling_is_refused(tmp_path, monkeypatch, filename):
    monkeypatch.setattr(driver_loader, "MAX_EXTRACTED_BYTES", 6)
    entries = {"a.txt": b"abc", "b.txt": b"defg"}
    payload = (
        _zip_with_entries(entries) if filename.endswith(".zip") else _tar_gz_with_entries(entries)
    )
    dest = tmp_path / "driver"
    with pytest.raises(PackageLimitError, match="more than 6 bytes uncompressed"):
        extract_driver_package(payload, filename, dest)
    assert not dest.exists()


@pytest.mark.parametrize("filename", ["pkg.zip", "pkg.tar.gz"])
def test_archive_one_entry_over_the_ceiling_is_refused(tmp_path, monkeypatch, filename):
    monkeypatch.setattr(driver_loader, "MAX_ARCHIVE_ENTRIES", 1)
    entries = {"a.txt": b"abc", "b.txt": b"defg"}
    payload = (
        _zip_with_entries(entries) if filename.endswith(".zip") else _tar_gz_with_entries(entries)
    )
    dest = tmp_path / "driver"
    with pytest.raises(PackageLimitError, match="more than 1 entries"):
        extract_driver_package(payload, filename, dest)
    assert not dest.exists()
