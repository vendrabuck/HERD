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
import tracemalloc
import zipfile
from pathlib import Path

import pytest
from app.services import driver_loader
from app.services.driver_loader import (
    MAX_ARCHIVE_ENTRIES,
    MAX_EXTRACTED_BYTES,
    MAX_TAR_READ_BYTES,
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


# --- A tar header record is never read past MAX_TAR_READ_BYTES (issue #1179) ---
#
# tarfile reads a pax extended header (x, g) or a GNU long name or long link (L, K)
# whole, in one read, before the member it describes exists to be checked. Before
# #1179 a 389 KB upload declaring a 400 MB pax header raised the process's peak RSS
# by about 500 MiB, and then extracted.

TAR_READ_REFUSAL = (
    f"Driver package has a tar header record over {MAX_TAR_READ_BYTES} bytes "
    "(a pax extended header or GNU long name); nothing was extracted"
)

_HEADER_TYPES = {
    "pax-x": tarfile.XHDTYPE,
    "pax-g": tarfile.XGLTYPE,
    "gnu-L": tarfile.GNUTYPE_LONGNAME,
    "gnu-K": tarfile.GNUTYPE_LONGLINK,
}


def _tar_gz_with_oversized_header(header_type: bytes, declared: int) -> bytes:
    """A gzip'd tar whose first header is `header_type` declaring `declared` bytes,
    followed by that many real (zero) bytes and a driver.py member, so a reader
    that honors the header does allocate it."""
    header = tarfile.TarInfo("././@header")
    header.type = header_type
    header.size = declared
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        gz.write(header.tobuf(format=tarfile.GNU_FORMAT))
        gz.write(b"\0" * declared)
        gz.write(b"\0" * ((512 - declared % 512) % 512))
        driver = tarfile.TarInfo("driver.py")
        data = VALID_L1_DRIVER.encode()
        driver.size = len(data)
        gz.write(driver.tobuf(format=tarfile.GNU_FORMAT))
        gz.write(data + b"\0" * ((512 - len(data) % 512) % 512))
        gz.write(b"\0" * 1024)
    return buf.getvalue()


def test_tar_read_bound_is_the_documented_constant():
    assert MAX_TAR_READ_BYTES == 1024 * 1024


@pytest.mark.parametrize("header_type", _HEADER_TYPES.values(), ids=_HEADER_TYPES)
def test_an_oversized_tar_header_record_is_refused_before_it_is_read(tmp_path, header_type):
    declared = 8 * MAX_TAR_READ_BYTES
    payload = _tar_gz_with_oversized_header(header_type, declared)
    dest = tmp_path / "driver"

    tracemalloc.start()
    try:
        with pytest.raises(PackageLimitError) as exc:
            extract_driver_package(payload, "pkg.tar.gz", dest)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert str(exc.value) == TAR_READ_REFUSAL
    assert not dest.exists()
    # The 8 MiB record was never read into memory.
    assert peak < 2 * MAX_TAR_READ_BYTES


def _pax_tar_gz_with_comment(comment_len: int) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT) as tf:
        data = VALID_L1_DRIVER.encode()
        info = tarfile.TarInfo("driver.py")
        info.size = len(data)
        info.pax_headers = {"comment": "c" * comment_len}
        tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_a_pax_header_just_under_the_read_bound_extracts(tmp_path):
    dest = tmp_path / "driver"
    extract_driver_package(_pax_tar_gz_with_comment(MAX_TAR_READ_BYTES - 4096), "p.tgz", dest)
    assert (dest / "driver.py").read_text() == VALID_L1_DRIVER


def test_a_well_formed_pax_header_just_over_the_read_bound_is_refused(tmp_path):
    dest = tmp_path / "driver"
    with pytest.raises(PackageLimitError) as exc:
        extract_driver_package(_pax_tar_gz_with_comment(MAX_TAR_READ_BYTES + 1), "p.tgz", dest)
    assert str(exc.value) == TAR_READ_REFUSAL
    assert not dest.exists()


@pytest.mark.parametrize("tar_format", [tarfile.PAX_FORMAT, tarfile.GNU_FORMAT], ids=["pax", "gnu"])
def test_long_member_names_still_extract(tmp_path, tar_format):
    name = "d" * 120 + "/" + "n" * 150 + ".py"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tar_format) as tf:
        for member_name, data in (("driver.py", VALID_L1_DRIVER.encode()), (name, b"x = 1\n")):
            info = tarfile.TarInfo(member_name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    dest = tmp_path / "driver"
    extract_driver_package(buf.getvalue(), "pkg.tar.gz", dest)
    assert (dest / name).read_bytes() == b"x = 1\n"


def test_member_data_larger_than_the_read_bound_extracts(tmp_path):
    """Member data is copied in small chunks, so the bound never refuses a big file."""
    data = bytes(range(256)) * (5 * MAX_TAR_READ_BYTES // 256)
    payload = _tar_gz_with_entries({"driver.py": VALID_L1_DRIVER.encode(), "blob.bin": data})
    dest = tmp_path / "driver"
    extract_driver_package(payload, "pkg.tar.gz", dest)
    assert (dest / "blob.bin").read_bytes() == data
