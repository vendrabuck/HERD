"""Branch coverage for app/services/extractor.py edge paths.

test_extractor.py covers the main happy paths and the top-level rejections;
this module pins the remaining branches: the .tar.gz extension alias, the
latin-1 decode fallback, the pdfplumber-missing ImportError, a parseable
multi-page PDF, and the per-member skips in the tgz reader (non-file member,
oversized member, char-budget break, and an extractfile()-returns-None member).

The last section pins the bounded tar read (issue #1192): a tar header record over
herd_common's MAX_TAR_READ_BYTES is refused before it is read into memory, an honest
archive extracts the same text as before, a member larger than the bound included,
and every failure of the gzip layer is an ExtractionError.
"""

import builtins
import gzip
import io
import struct
import tarfile
import tracemalloc
import zlib

import pytest
from app import config as config_module
from app.services import extractor
from app.services.extractor import (
    ExtractionError,
    _decode_text,
    _extension,
    _extract_pdf,
    _extract_tgz,
    extract_files,
)
from herd_common.bounded_tar import MAX_TAR_READ_BYTES

# --- _extension: .tar.gz aliases to .tgz (line 37-38) ---


def test_extension_tar_gz_alias():
    assert _extension("bundle.tar.gz") == ".tgz"


def test_extension_no_dot_returns_empty():
    assert _extension("README") == ""


def test_extract_files_accepts_tar_gz_suffix():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="inner.txt")
        content = b"double suffix bundle"
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    result = extract_files([("support.tar.gz", buf.getvalue())])
    assert "double suffix bundle" in result[0].text


# --- _decode_text: latin-1 fallback on invalid utf-8 (lines 46-47) ---


def test_decode_text_falls_back_to_latin1():
    # 0xFF is invalid as a standalone utf-8 byte, valid latin-1 (y-diaeresis).
    decoded = _decode_text(b"caf\xff")
    assert decoded.startswith("caf")
    assert len(decoded) == 4


# --- _extract_pdf: pdfplumber missing -> ExtractionError (lines 53-54) ---


def test_extract_pdf_missing_pdfplumber_raises(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pdfplumber":
            raise ImportError("no pdfplumber")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ExtractionError, match="pdfplumber is not installed"):
        _extract_pdf(b"%PDF-1.4 whatever")


# --- _extract_pdf: a parseable multi-page document collects page text (59-65) ---


def test_extract_pdf_collects_page_text(monkeypatch):
    class _Page:
        def __init__(self, text):
            self._text = text

        def extract_text(self):
            return self._text

    class _PDF:
        # Page 2 returns "" so the `if text:` guard (line 61) skips it.
        pages = [_Page("page one body"), _Page("")]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _FakePlumber:
        @staticmethod
        def open(_fileobj):
            return _PDF()

    import sys

    monkeypatch.setitem(sys.modules, "pdfplumber", _FakePlumber)
    out = _extract_pdf(b"%PDF-fake")
    assert out == "page one body"


# --- _extract_tgz member-skip branches (76, 80, 82, 85) ---


def _make_tgz(members: list[tuple[str, bytes]], *, include_dir: bool = False) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        if include_dir:
            dir_info = tarfile.TarInfo(name="subdir")
            dir_info.type = tarfile.DIRTYPE
            tar.addfile(dir_info)
        for name, content in members:
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def test_tgz_skips_directory_member():
    """A non-file (directory) member is skipped (line 76)."""
    data = _make_tgz([("keep.txt", b"real content")], include_dir=True)
    result = extract_files([("b.tgz", data)])
    assert "real content" in result[0].text
    assert "subdir" not in result[0].text


def test_tgz_skips_oversized_member(monkeypatch):
    """A member larger than upload_max_file_bytes is skipped (line 80).

    Calls _extract_tgz directly: the per-member size cap and the whole-file
    pre-check in extract_files share one setting, so a member can only exceed
    the cap if the (compressed) archive does too. Driving the inner reader
    directly isolates the member-skip branch.
    """
    monkeypatch.setattr(config_module.settings, "upload_max_file_bytes", 8)
    data = _make_tgz([("small.txt", b"tiny"), ("big.txt", b"x" * 50)])
    out = _extract_tgz(data, remaining_budget=10_000)
    assert "tiny" in out
    assert "x" * 50 not in out


def test_tgz_stops_at_budget():
    """When the remaining char budget is exhausted, the member loop breaks (82).

    Calls _extract_tgz directly: extract_files cuts every file to the remaining
    budget afterwards, so through it the output is the same whether or not the
    loop broke. Read directly, the second member's header proves it was read.
    """
    # Budget of 5 chars: the first text member fills it, the second is never read.
    data = _make_tgz([("a.txt", b"first member text"), ("b.txt", b"second member text")])
    out = _extract_tgz(data, remaining_budget=5)
    # Only the first member contributed; the loop broke before the second.
    assert out == "--- a.txt ---\nfirst member text"
    assert "--- b.txt ---" not in out


def test_tgz_skips_member_when_extractfile_returns_none(monkeypatch):
    """If tar.extractfile() returns None for a member, it is skipped (line 85).

    Build the archive first, THEN patch tarfile.open to return a wrapper whose
    extractfile() yields None, and drive _extract_tgz directly so the write-mode
    open used to build the fixture is not intercepted.
    """
    data = _make_tgz([("a.txt", b"unreadable")])

    real_open = tarfile.open

    class _NoneExtractTar:
        def __init__(self, inner):
            self._inner = inner

        def __enter__(self):
            self._inner.__enter__()
            return self

        def __exit__(self, *exc):
            return self._inner.__exit__(*exc)

        def __iter__(self):
            return iter(self._inner)

        def extractfile(self, _member):
            return None

    def fake_open(*args, **kwargs):
        return _NoneExtractTar(real_open(*args, **kwargs))

    monkeypatch.setattr(extractor.tarfile, "open", fake_open)
    out = _extract_tgz(data, remaining_budget=10_000)
    # extractfile returned None for every member, so no text was collected.
    assert out == ""


# --- The tgz is read through the bounded tar stream (issue #1192) ---
#
# tarfile reads a pax extended header (x, g) or a GNU long name or long link (L, K)
# whole, in one read, before the member it describes exists to be checked. Before
# #1192 a 65 KB upload declaring a 64 MiB header raised the process's peak traced
# memory by about 128 MiB, and then extracted.

TGZ_READ_REFUSAL = (
    "The tgz archive has a tar header record over 1048576 bytes "
    "(a pax extended header or GNU long name); nothing was extracted"
)
UNREADABLE_TGZ = "Failed to read tgz archive (ReadError)"

_HEADER_TYPES = {
    "pax-x": tarfile.XHDTYPE,
    "pax-g": tarfile.XGLTYPE,
    "gnu-L": tarfile.GNUTYPE_LONGNAME,
    "gnu-K": tarfile.GNUTYPE_LONGLINK,
}


def _member_bytes(name: str, content: bytes) -> bytes:
    """One tar member as raw bytes: its header block, its data, and the padding."""
    info = tarfile.TarInfo(name)
    info.size = len(content)
    padding = b"\0" * ((512 - len(content) % 512) % 512)
    return info.tobuf(format=tarfile.GNU_FORMAT) + content + padding


def _tgz_with_oversized_header(header_type: bytes, declared: int) -> bytes:
    """A gzip'd tar whose first header is `header_type` declaring `declared` bytes,
    followed by that many real (zero) bytes and a text member, so a reader that
    honors the header does allocate it."""
    header = tarfile.TarInfo("././@header")
    header.type = header_type
    header.size = declared
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        gz.write(header.tobuf(format=tarfile.GNU_FORMAT))
        gz.write(b"\0" * declared)
        gz.write(b"\0" * ((512 - declared % 512) % 512))
        gz.write(_member_bytes("notes.txt", b"hello"))
        gz.write(b"\0" * 1024)
    return buf.getvalue()


def _pax_tgz_with_comment(comment_len: int) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
        info = tarfile.TarInfo("notes.txt")
        info.size = 5
        info.pax_headers = {"comment": "c" * comment_len}
        tar.addfile(info, io.BytesIO(b"hello"))
    return buf.getvalue()


def _numbered_lines(total_bytes: int) -> bytes:
    """ASCII text of exactly `total_bytes` bytes in which no two lines are equal, so
    a chunk read twice, dropped, or out of order changes the text."""
    lines = b"".join(b"line %07d of an honest log\n" % i for i in range(total_bytes // 28 + 1))
    return lines[:total_bytes]


def test_tgz_read_refusal_and_member_chunk_follow_the_shared_bound():
    assert extractor.TGZ_READ_LIMIT_DETAIL == TGZ_READ_REFUSAL
    # A member is read in chunks through BoundedTarStream, which refuses a single
    # read over MAX_TAR_READ_BYTES: a larger chunk would refuse an honest member.
    assert 0 < extractor._MEMBER_READ_CHUNK_BYTES <= MAX_TAR_READ_BYTES


@pytest.mark.parametrize("header_type", _HEADER_TYPES.values(), ids=_HEADER_TYPES)
def test_tgz_header_record_over_the_read_bound_is_refused_before_it_is_read(header_type):
    declared = 8 * MAX_TAR_READ_BYTES
    data = _tgz_with_oversized_header(header_type, declared)
    # The upload itself is far under the per-file cap: only the header is large.
    assert len(data) < 64 * 1024

    tracemalloc.start()
    try:
        with pytest.raises(ExtractionError) as exc:
            extract_files([("bundle.tgz", data)])
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert str(exc.value) == TGZ_READ_REFUSAL
    # The 8 MiB record was never read into memory.
    assert peak < 2 * MAX_TAR_READ_BYTES


def test_tgz_pax_header_just_under_the_read_bound_extracts():
    result = extract_files([("bundle.tgz", _pax_tgz_with_comment(MAX_TAR_READ_BYTES - 4096))])
    assert result[0].text == "--- notes.txt ---\nhello"
    assert result[0].truncated is False


def test_tgz_well_formed_pax_header_just_over_the_read_bound_is_refused():
    with pytest.raises(ExtractionError) as exc:
        extract_files([("bundle.tgz", _pax_tgz_with_comment(MAX_TAR_READ_BYTES + 1))])
    assert str(exc.value) == TGZ_READ_REFUSAL


@pytest.mark.parametrize("tar_format", [tarfile.PAX_FORMAT, tarfile.GNU_FORMAT], ids=["pax", "gnu"])
def test_tgz_long_member_names_still_extract(tar_format):
    """An honest pax header or GNU long name is a few hundred bytes and reads as before."""
    name = "d" * 120 + "/" + "n" * 150 + ".txt"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tar_format) as tar:
        info = tarfile.TarInfo(name)
        info.size = 14
        tar.addfile(info, io.BytesIO(b"long name body"))
    result = extract_files([("bundle.tgz", buf.getvalue())])
    assert result[0].text == f"--- {name} ---\nlong name body"


def test_tgz_small_archive_extracts_the_same_text_as_before():
    """Member filter, header line, and join are unchanged by the bounded read."""
    data = _make_tgz(
        [
            ("a.txt", b"alpha\n"),
            ("ignored.bin", b"\x00\x01\x02"),
            ("empty.txt", b""),
            ("sub/b.log", b"beta"),
            ("c.yaml", "caf\u00e9: 1\n".encode()),
        ],
        include_dir=True,
    )
    result = extract_files([("bundle.tgz", data)])
    assert result[0].text == (
        "--- a.txt ---\nalpha\n\n\n--- sub/b.log ---\nbeta\n\n--- c.yaml ---\ncaf\u00e9: 1\n"
    )
    assert result[0].truncated is False


def test_tgz_member_larger_than_the_read_bound_extracts_whole(monkeypatch):
    """A text member may be as large as the per-file cap, well over the read bound.

    One read of a member's data becomes one read of that size from the bounded
    stream, so reading it in a single call would refuse this honest archive as if
    it held an oversized header. The default character budget keeps every read
    under the bound, so the budget is raised here as an operator may raise it.
    """
    content = _numbered_lines(2_400_000)
    assert MAX_TAR_READ_BYTES < len(content) < config_module.settings.upload_max_file_bytes
    data = _make_tgz([("big.log", content)])
    monkeypatch.setattr(config_module.settings, "upload_max_extracted_chars", 10_000_000)

    result = extract_files([("bundle.tgz", data)])

    assert result[0].text == "--- big.log ---\n" + content.decode("ascii")
    assert result[0].truncated is False


def test_tgz_member_larger_than_the_budget_is_read_to_the_same_cut():
    """A member over the remaining budget is read to budget plus 1024 bytes, no further.

    The cut is over the read bound and is not a multiple of the member chunk, so the
    chunked read must stop inside a chunk at the byte a single read stopped at.
    """
    content = _numbered_lines(3_000_000)
    budget = 2_000_000
    cut = budget + 1024
    assert MAX_TAR_READ_BYTES < cut < len(content)
    assert cut % extractor._MEMBER_READ_CHUNK_BYTES != 0

    out = _extract_tgz(_make_tgz([("big.log", content)]), remaining_budget=budget)

    assert out == "--- big.log ---\n" + content[:cut].decode("ascii")


def test_tgz_second_member_is_cut_by_what_the_first_member_used():
    first = _numbered_lines(500)
    second = _numbered_lines(300_000)
    budget = 1000
    first_chunk = "--- a.txt ---\n" + first.decode("ascii")
    cut = budget - len(first_chunk) + 1024
    assert 0 < cut < len(second)

    out = _extract_tgz(_make_tgz([("a.txt", first), ("b.txt", second)]), remaining_budget=budget)

    assert out == first_chunk + "\n\n" + "--- b.txt ---\n" + second[:cut].decode("ascii")


# A gzip member header: magic, deflate, no flags, no mtime, no extra flags, unknown OS.
_GZIP_HEADER = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\xff"


def _gzip_of_stored_blocks(payload: bytes, tail: bytes) -> bytes:
    """A gzip body holding `payload` in stored deflate blocks, none of them final,
    followed by `tail`. Written by hand so the bytes do not depend on the zlib build."""
    out = [_GZIP_HEADER]
    for start in range(0, len(payload), 65535):
        block = payload[start : start + 65535]
        out.append(b"\x00" + struct.pack("<HH", len(block), len(block) ^ 0xFFFF) + block)
    out.append(tail)
    return b"".join(out)


def _tgz_cut_inside_member_data(name: str, tail: bytes) -> bytes:
    """A member declaring 100,000 bytes whose stream holds 50,000 of them, then `tail`."""
    return _gzip_of_stored_blocks(_member_bytes(name, _numbered_lines(100_000))[:50_512], tail)


def _tgz_with_a_wrong_checksum() -> bytes:
    """A tar with no end-of-archive blocks, so the reader runs into the gzip trailer,
    whose CRC is wrong."""
    data = bytearray(gzip.compress(_member_bytes("a.txt", b"checksum body")))
    data[-8] ^= 0xFF
    return bytes(data)


# The reserved deflate block type: zlib refuses it with "invalid block type".
_INVALID_DEFLATE_BLOCK = b"\x07"

_GZIP_LAYER_FAILURES = {
    "not-gzip": (lambda: b"not a real tgz", gzip.BadGzipFile),
    "gzip-magic-only": (lambda: b"\x1f\x8b", EOFError),
    "truncated-in-member-data": (lambda: _tgz_cut_inside_member_data("a.txt", b""), EOFError),
    "corrupt-in-member-data": (
        lambda: _tgz_cut_inside_member_data("a.txt", _INVALID_DEFLATE_BLOCK),
        zlib.error,
    ),
    "corrupt-in-skipped-member": (
        lambda: _tgz_cut_inside_member_data("skipped.bin", _INVALID_DEFLATE_BLOCK),
        zlib.error,
    ),
    "wrong-checksum": (_tgz_with_a_wrong_checksum, gzip.BadGzipFile),
}


@pytest.mark.parametrize(
    ("build", "gzip_error"), _GZIP_LAYER_FAILURES.values(), ids=_GZIP_LAYER_FAILURES
)
def test_tgz_gzip_layer_failure_is_an_extraction_error(build, gzip_error):
    """The gzip stream is opened by hand and read lazily, so a body that is not gzip,
    ends early, is corrupt, or fails its checksum can fail at the open, at a header,
    inside a member's data, or while a member is skipped. Each one is the same
    ExtractionError a body that is not gzip always was, never another exception."""
    with pytest.raises(ExtractionError) as exc:
        _extract_tgz(build(), remaining_budget=10_000_000)

    assert str(exc.value) == UNREADABLE_TGZ
    assert type(exc.value.__cause__) is tarfile.ReadError
    # The case failed in the gzip layer, in the way its name says.
    assert type(exc.value.__cause__.__cause__) is gzip_error


def test_tgz_honest_stored_block_stream_extracts():
    """The control for the hand-built streams above: complete, the same bytes extract."""
    content = _numbered_lines(100_000)
    payload = _member_bytes("a.txt", content) + b"\0" * 1024
    # A final, empty stored block, then the gzip trailer: CRC-32 and length.
    end_of_stream = b"\x01\x00\x00\xff\xff" + struct.pack("<II", zlib.crc32(payload), len(payload))
    data = _gzip_of_stored_blocks(payload, end_of_stream)
    assert gzip.decompress(data) == payload

    out = _extract_tgz(data, remaining_budget=10_000_000)

    assert out == "--- a.txt ---\n" + content.decode("ascii")
