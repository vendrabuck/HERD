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

The sections after it pin the two ceilings on the walk (issue #1195): the member past
MAX_TGZ_ENTRIES is refused before another header is read, and no record of any type
moves the decompressed stream past MAX_TGZ_DECOMPRESSED_BYTES; and the member name
(issue #1196): a name that is not UTF-8 enters the text with U+FFFD in place of each
undecodable byte.
"""

import builtins
import gzip
import io
import json
import struct
import tarfile
import tracemalloc
import zlib

import pytest
from app import config as config_module
from app.services import extractor
from app.services.extractor import (
    ExtractionError,
    _CeilingStream,
    _decode_text,
    _extension,
    _extract_pdf,
    _extract_tgz,
    _safe_member_name,
    _TgzLimitError,
    extract_files,
    render_file_context,
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


# --- The walk over a tgz is bounded (issue #1195) ---
#
# Before #1195 the walk stopped only at the character budget, which text members alone
# advance, or at the end of the archive. An 894 KB upload of 400,000 empty members took
# 7.7 s and raised the process's resident memory from 57 MiB to 226 MiB, and a 1 MiB
# upload holding one skipped member of 1 GiB of zeros took 2.1 s, all of it on the
# event loop.

ENTRY_REFUSAL = "The tgz archive has more than 10000 entries; nothing was extracted"
BYTES_REFUSAL = "The tgz archive extends past 104857600 bytes uncompressed; nothing was extracted"

_MIB = 1024 * 1024
_NOTES = "--- notes.txt ---\nhello"


def _header_block(name: str, size: int = 0, type_: bytes = tarfile.REGTYPE) -> bytes:
    """One 512-byte tar header declaring `size` bytes, with no data after it."""
    info = tarfile.TarInfo(name)
    info.size = size
    info.type = type_
    return info.tobuf(format=tarfile.GNU_FORMAT)


def _gzip_members(*pieces: tuple[bytes, int]) -> bytes:
    """A gzip file made of several gzip members, which a reader sees as one stream.
    Each piece is compressed once and repeated `times` times, so a stream of a hundred
    megabytes is built without compressing a hundred megabytes."""
    return b"".join(gzip.compress(piece) * times for piece, times in pieces if piece)


def _zeros(count: int) -> list[tuple[bytes, int]]:
    """`count` zero bytes as pieces for _gzip_members."""
    whole, rest = divmod(count, _MIB)
    return [(b"\0" * _MIB, whole), (b"\0" * rest, 1)]


_NOTES_MEMBER_AND_END = _member_bytes("notes.txt", b"hello") + b"\0" * 1024


def _tgz_with_a_large_member(
    name: str, size: int, *, type_: bytes = tarfile.REGTYPE, with_data: bool = True
) -> bytes:
    """A member declaring `size` bytes, its data (zeros) unless `with_data` is False,
    then notes.txt and the end of the archive. `size` is a multiple of 512."""
    assert size % 512 == 0
    data = _zeros(size) if with_data else []
    return _gzip_members((_header_block(name, size, type_), 1), *data, (_NOTES_MEMBER_AND_END, 1))


@pytest.fixture
def stream_reached(monkeypatch):
    """How far the extractor moved each decompressed stream it opened: the stream's
    position when it was closed. It only records; the stream reads as it always does."""
    reached: list[int] = []

    class _RecordingGzipFile(gzip.GzipFile):
        def close(self):
            if self.fileobj is not None:
                reached.append(self.tell())
            super().close()

    monkeypatch.setattr(extractor.gzip, "GzipFile", _RecordingGzipFile)
    return reached


def test_tgz_ceilings_and_their_refusals_are_the_pinned_values():
    assert extractor.MAX_TGZ_ENTRIES == 10_000
    assert extractor.MAX_TGZ_DECOMPRESSED_BYTES == 100 * _MIB
    assert extractor.TGZ_ENTRY_LIMIT_DETAIL == ENTRY_REFUSAL
    assert extractor.TGZ_BYTES_LIMIT_DETAIL == BYTES_REFUSAL


# Each kind of member the walk passes over without reading: none of them adds to the
# character budget, so before #1195 nothing counted them.
_UNREAD_MEMBERS = {
    "empty-file": _header_block("a.bin"),
    "empty-text-file": _header_block("a.txt"),
    "directory": _header_block("d", type_=tarfile.DIRTYPE),
    "symlink": _header_block("l", type_=tarfile.SYMTYPE),
    "fifo": _header_block("f", type_=tarfile.FIFOTYPE),
    "unknown-type": _header_block("u", type_=b"Z"),
    "non-text-file-with-data": _member_bytes("a.bin", b"\x00\x01"),
}


def test_tgz_with_exactly_the_entry_ceiling_extracts(stream_reached):
    """Ten thousand members, of every kind the walk passes over, and the last one text."""
    kinds = list(_UNREAD_MEMBERS.values())
    each, extra = divmod(extractor.MAX_TGZ_ENTRIES - 1, len(kinds))
    filler = b"".join(kinds) * each + kinds[0] * extra
    data = _gzip_members((filler, 1), (_NOTES_MEMBER_AND_END, 1))

    result = extract_files([("bundle.tgz", data)])

    assert result[0].text == _NOTES
    assert result[0].truncated is False
    # The whole archive was walked, to the end of its first end-of-archive block.
    assert stream_reached == [len(filler) + 1024 + 512]


@pytest.mark.parametrize("member", _UNREAD_MEMBERS.values(), ids=_UNREAD_MEMBERS)
def test_tgz_one_entry_over_the_ceiling_is_refused_whatever_the_members_are(member, monkeypatch):
    """Every member the iteration yields counts, whatever its type or extension, and
    the member that passes the ceiling may be of any kind: here it is one the walk
    would not read. The ceiling is lowered so each kind is tried on both sides of it
    quickly; the tests around this one use the real ceiling."""
    ceiling = 20
    monkeypatch.setattr(extractor, "MAX_TGZ_ENTRIES", ceiling)

    def tgz(count: int) -> bytes:
        tar_bytes = _member_bytes("notes.txt", b"hello") + member * (count - 1)
        return gzip.compress(tar_bytes + b"\0" * 1024)

    assert _extract_tgz(tgz(ceiling), remaining_budget=10_000) == _NOTES
    with pytest.raises(ExtractionError) as exc:
        _extract_tgz(tgz(ceiling + 1), remaining_budget=10_000)
    assert str(exc.value) == ENTRY_REFUSAL


def test_tgz_one_entry_over_the_ceiling_is_refused():
    """The text member is the 10,001st: nothing is extracted, the file is refused."""
    data = _gzip_members(
        (_header_block("a.bin") * extractor.MAX_TGZ_ENTRIES, 1), (_NOTES_MEMBER_AND_END, 1)
    )
    with pytest.raises(ExtractionError) as exc:
        extract_files([("bundle.tgz", data)])
    assert str(exc.value) == ENTRY_REFUSAL


def test_tgz_entry_flood_is_refused_at_the_member_past_the_ceiling(stream_reached):
    """Sixty thousand empty members in a 134 KB upload. The walk reads the header of
    the member past the ceiling, which is what shows the archive is over it, and no
    header after that one, so what tarfile keeps per member stays bounded."""
    members = 60_000
    data = gzip.compress(_header_block("a.bin") * members + b"\0" * 1024)
    assert len(data) < 256 * 1024

    tracemalloc.start()
    try:
        with pytest.raises(ExtractionError) as exc:
            extract_files([("bundle.tgz", data)])
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert str(exc.value) == ENTRY_REFUSAL
    assert stream_reached == [(extractor.MAX_TGZ_ENTRIES + 1) * 512]
    # About 4 MiB with the ceiling; walking all sixty thousand holds about 25 MiB.
    assert peak < 12 * _MIB


def test_tgz_walk_ends_at_a_text_member_after_the_budget_and_not_before(monkeypatch):
    """The character budget ends the walk only at the next text member with data. The
    members before that one are still walked and counted, so a spent budget does not
    excuse an archive from the ceiling; members after it are never reached."""
    monkeypatch.setattr(extractor, "MAX_TGZ_ENTRIES", 5)
    first = _member_bytes("a.txt", b"first member text")
    empties = _header_block("a.bin") * 10
    end = b"\0" * 1024

    stopped_by_the_budget = gzip.compress(first + _member_bytes("b.txt", b"x") + empties + end)
    assert _extract_tgz(stopped_by_the_budget, remaining_budget=5) == (
        "--- a.txt ---\nfirst member text"
    )

    no_text_member_follows = gzip.compress(first + empties + end)
    with pytest.raises(ExtractionError) as exc:
        _extract_tgz(no_text_member_follows, remaining_budget=5)
    assert str(exc.value) == ENTRY_REFUSAL


def test_ceiling_stream_refuses_a_read_or_seek_past_the_ceiling_before_moving(monkeypatch):
    """The bound is on the position, to the byte: a read or seek ending at the ceiling
    is served, one ending a byte past it is refused, and a refusal moves nothing."""
    monkeypatch.setattr(extractor, "MAX_TGZ_DECOMPRESSED_BYTES", 1000)
    payload = bytes(range(256)) * 8
    with gzip.GzipFile(fileobj=io.BytesIO(gzip.compress(payload)), mode="rb") as gz:
        stream = _CeilingStream(gz)

        assert stream.read(600) == payload[:600]
        with pytest.raises(_TgzLimitError) as exc:
            stream.read(401)
        assert str(exc.value) == BYTES_REFUSAL
        assert stream.tell() == gz.tell() == 600
        assert stream.read(400) == payload[600:1000]
        with pytest.raises(_TgzLimitError):
            stream.read(1)
        assert gz.tell() == 1000

        assert stream.seek(0) == 0
        assert stream.seek(1000) == 1000
        assert stream.seek(10) == 10
        with pytest.raises(_TgzLimitError) as exc:
            stream.seek(1001)
        assert str(exc.value) == BYTES_REFUSAL
        assert gz.tell() == 10
        # tarfile seeks from the start only; no other seek goes through unchecked.
        for whence in (io.SEEK_CUR, io.SEEK_END):
            with pytest.raises(io.UnsupportedOperation):
                stream.seek(0, whence)
        assert gz.tell() == 10


def _pax_record(keyword: bytes, value: bytes) -> bytes:
    body = b" " + keyword + b"=" + value + b"\n"
    length = len(body) + 1
    while len(str(length)) + len(body) != length:
        length = len(str(length)) + len(body)
    return str(length).encode() + body


def _pax_size_override(size: int) -> bytes:
    """A pax extended header giving the next member a size its own header does not."""
    record = _pax_record(b"size", str(size).encode())
    return _header_block("././@PaxHeader", len(record), tarfile.XHDTYPE) + record.ljust(512, b"\0")


# Every place the bytes of a decompressed tar can be, each crossing a 64 KiB ceiling
# in a different record. A sum of member sizes sees some of them and misses the rest:
# the header and padding blocks of many small members, and every pax header and GNU
# long name.
_SMALL_CEILING = 64 * 1024
_RECORDS_PAST_A_SMALL_CEILING = {
    "skipped-member-data": _header_block("a.bin", 70_144) + b"s" * 70_144,
    "text-member-data": _header_block("a.txt", 70_144) + b"t" * 70_144,
    "many-small-members": _member_bytes("a.bin", b"m" * 512) * 80,
    "pax-extended-header": _header_block("././@PaxHeader", 70_144, tarfile.XHDTYPE)
    + b"\0" * 70_144
    + _header_block("a.bin"),
    "pax-global-header": _header_block("././@PaxHeader", 70_144, tarfile.XGLTYPE)
    + b"\0" * 70_144
    + _header_block("a.bin"),
    "gnu-long-name": _header_block("././@LongLink", 70_144, tarfile.GNUTYPE_LONGNAME)
    + b"n" * 70_143
    + b"\0"
    + _header_block("a.bin"),
    "gnu-long-link": _header_block("././@LongLink", 70_144, tarfile.GNUTYPE_LONGLINK)
    + b"n" * 70_143
    + b"\0"
    + _header_block("a.bin"),
    "pax-headers-of-empty-members": (
        _header_block("././@PaxHeader", 8192, tarfile.XHDTYPE)
        + b"\0" * 8192
        + _header_block("a.bin")
    )
    * 10,
    # tarfile reads a run of such headers inside one step of the iteration, so the
    # walk sees no member between them at which it could check anything.
    "consecutive-pax-headers-before-one-member": (
        _header_block("././@PaxHeader", 8192, tarfile.XHDTYPE) + b"\0" * 8192
    )
    * 10
    + _header_block("a.bin"),
    "size-in-a-pax-record": _pax_size_override(10**11) + _header_block("a.bin"),
    "size-the-data-does-not-back": _header_block("a.bin", 10**9),
    "data-of-an-unknown-member-type": _header_block("u", 70_144, b"Z") + b"u" * 70_144,
}


@pytest.mark.parametrize(
    "records", _RECORDS_PAST_A_SMALL_CEILING.values(), ids=_RECORDS_PAST_A_SMALL_CEILING
)
def test_tgz_no_record_type_moves_the_stream_past_the_byte_ceiling(
    records, monkeypatch, stream_reached
):
    """Whatever kind of record the bytes are in, the walk is refused and the stream
    stays at or under the ceiling. The ceiling is lowered to 64 KiB so every kind is
    tried quickly; the wording is the constant's, so it still names the real one."""
    monkeypatch.setattr(extractor, "MAX_TGZ_DECOMPRESSED_BYTES", _SMALL_CEILING)
    monkeypatch.setattr(config_module.settings, "upload_max_extracted_chars", 10_000_000)
    data = gzip.compress(records + _NOTES_MEMBER_AND_END)

    with pytest.raises(ExtractionError) as exc:
        extract_files([("bundle.tgz", data)])

    assert str(exc.value) == BYTES_REFUSAL
    assert len(stream_reached) == 1
    assert stream_reached[0] <= _SMALL_CEILING


def test_tgz_records_under_a_small_ceiling_still_extract(monkeypatch):
    """The control for the cases above: the same kinds of record, sized to end under
    the lowered ceiling, extract, so those refusals come from the ceiling alone."""
    monkeypatch.setattr(extractor, "MAX_TGZ_DECOMPRESSED_BYTES", _SMALL_CEILING)
    records = (
        _header_block("a.bin", 8192)
        + b"s" * 8192
        + _header_block("././@PaxHeader", 8192, tarfile.XHDTYPE)
        + b"\0" * 8192
        + _header_block("a.bin")
        + _header_block("././@LongLink", 8192, tarfile.GNUTYPE_LONGNAME)
        + b"n" * 8191
        + b"\0"
        + _header_block("a.bin")
        + _member_bytes("first.txt", b"t" * 8192)
    )
    assert len(records) + len(_NOTES_MEMBER_AND_END) < _SMALL_CEILING

    out = _extract_tgz(gzip.compress(records + _NOTES_MEMBER_AND_END), remaining_budget=100_000)

    assert out == "--- first.txt ---\n" + "t" * 8192 + "\n\n" + _NOTES


# The stream positions of an archive holding one large member, then notes.txt: the
# large member's header, its data, notes.txt's header and one data block, and the
# first end-of-archive block, which is the last thing the reader reads.
_BLOCKS_AROUND_THE_LARGE_MEMBER = 4 * 512


def test_tgz_whose_first_end_block_ends_at_the_byte_ceiling_extracts(stream_reached):
    """The bound is exact to the tar block. This archive's first end-of-archive block
    ends at byte 104,857,600 and the reader passes over all of it."""
    size = extractor.MAX_TGZ_DECOMPRESSED_BYTES - _BLOCKS_AROUND_THE_LARGE_MEMBER
    data = _tgz_with_a_large_member("big.bin", size)
    assert len(data) < 256 * 1024

    result = extract_files([("bundle.tgz", data)])

    assert result[0].text == _NOTES
    assert stream_reached == [extractor.MAX_TGZ_DECOMPRESSED_BYTES]


def test_tgz_one_block_past_the_byte_ceiling_is_refused(stream_reached):
    """One 512-byte block more and the last read would end past the ceiling."""
    size = extractor.MAX_TGZ_DECOMPRESSED_BYTES - _BLOCKS_AROUND_THE_LARGE_MEMBER + 512
    data = _tgz_with_a_large_member("big.bin", size)

    with pytest.raises(ExtractionError) as exc:
        extract_files([("bundle.tgz", data)])

    assert str(exc.value) == BYTES_REFUSAL
    assert stream_reached == [extractor.MAX_TGZ_DECOMPRESSED_BYTES]


_OVER_THE_BYTE_CEILING = 101 * _MIB
_MEMBERS_PASSED_OVER = {
    # Not a text extension, so the walk skips it.
    "non-text-member": lambda: _tgz_with_a_large_member("big.bin", _OVER_THE_BYTE_CEILING),
    # A text extension, but larger than the per-file cap, so the walk skips it.
    "text-member-over-the-file-cap": lambda: _tgz_with_a_large_member(
        "big.log", _OVER_THE_BYTE_CEILING
    ),
    "unknown-member-type": lambda: _tgz_with_a_large_member(
        "big", _OVER_THE_BYTE_CEILING, type_=b"Z"
    ),
    # The issue's measured upload, with the size its header declares but no data.
    "declared-size-with-no-data": lambda: _tgz_with_a_large_member(
        "big.bin", 1024 * _MIB, with_data=False
    ),
}


@pytest.mark.parametrize("build", _MEMBERS_PASSED_OVER.values(), ids=_MEMBERS_PASSED_OVER)
def test_tgz_member_ending_past_the_byte_ceiling_is_refused_before_it_is_decompressed(
    build, stream_reached
):
    """tarfile passes over a member's data by seeking forward in the gzip stream,
    which decompresses and discards all of it. The member's end is past the ceiling,
    so that seek is refused: the stream stays where the member's header ended."""
    data = build()
    assert len(data) < 256 * 1024

    with pytest.raises(ExtractionError) as exc:
        extract_files([("bundle.tgz", data)])

    assert str(exc.value) == BYTES_REFUSAL
    assert stream_reached == [512]


def test_tgz_flood_of_pax_headers_is_refused_at_the_byte_ceiling(stream_reached):
    """A hundred and twenty pax extended headers of just under 1 MiB each, every one
    followed by an empty member: 126 MiB of stream in a 138 KB upload. Each header is
    under the single-read bound, the members are far under the entry ceiling, and the
    member sizes sum to zero, so only a bound on the stream position sees them. At the
    5 MiB upload cap the same flood is 4.7 GiB and took 9.7 s with no such bound."""
    header_bytes = MAX_TAR_READ_BYTES - 512
    pair = (
        _header_block("././@PaxHeader", header_bytes, tarfile.XHDTYPE)
        + b"\0" * header_bytes
        + _header_block("a.bin")
    )
    pairs = 120
    assert pairs * len(pair) > extractor.MAX_TGZ_DECOMPRESSED_BYTES
    assert pairs < extractor.MAX_TGZ_ENTRIES
    data = _gzip_members((pair, pairs), (_NOTES_MEMBER_AND_END, 1))
    assert len(data) < 256 * 1024

    with pytest.raises(ExtractionError) as exc:
        extract_files([("bundle.tgz", data)])

    assert str(exc.value) == BYTES_REFUSAL
    # Ninety-nine whole pairs fit; the hundredth pair's header data would end past it.
    assert stream_reached == [99 * len(pair) + 512]
    assert stream_reached[0] <= extractor.MAX_TGZ_DECOMPRESSED_BYTES


# --- A member name that is not UTF-8 (issue #1196) ---
#
# tarfile decodes a name byte that is not valid UTF-8 to a lone surrogate. Before
# #1196 the name went into the text as it was, and the provider call then failed to
# encode its request body.


def _member_named(raw_name: bytes, content: bytes, tar_format: int = tarfile.GNU_FORMAT) -> bytes:
    """One tar member whose name is exactly `raw_name`, valid UTF-8 or not."""
    info = tarfile.TarInfo(raw_name.decode("utf-8", "surrogateescape"))
    info.size = len(content)
    header = info.tobuf(format=tar_format, encoding="utf-8", errors="surrogateescape")
    assert raw_name in header
    return header + content + b"\0" * ((512 - len(content) % 512) % 512)


_NAMES_THAT_ARE_NOT_UTF8 = {
    "one-bad-byte": (b"bad\xff name.txt", "bad\ufffd name.txt"),
    "two-bad-bytes": (b"\xfe\xff.txt", "\ufffd\ufffd.txt"),
    "cut-multibyte-character": (b"caf\xc3.txt", "caf\ufffd.txt"),
    "latin-1-name": (b"r\xe9sum\xe9.txt", "r\ufffdsum\ufffd.txt"),
    "encoded-surrogate": (b"s\xed\xb3\xbf.txt", "s\ufffd\ufffd\ufffd.txt"),
    "bad-byte-in-a-directory": (b"d\xff/ok.txt", "d\ufffd/ok.txt"),
    "bad-byte-beside-honest-non-ascii": (
        "caf\u00e9-".encode() + b"\xff.txt",
        "caf\u00e9-\ufffd.txt",
    ),
}


@pytest.mark.parametrize(
    "tar_format", [tarfile.GNU_FORMAT, tarfile.PAX_FORMAT], ids=["gnu", "pax-record"]
)
@pytest.mark.parametrize(
    ("raw_name", "shown"), _NAMES_THAT_ARE_NOT_UTF8.values(), ids=_NAMES_THAT_ARE_NOT_UTF8
)
def test_tgz_member_name_that_is_not_utf8_enters_the_text_with_replacement_characters(
    raw_name, shown, tar_format
):
    """Each undecodable byte of the name is shown as U+FFFD, whether the name is in
    the member's header or in a pax path record, and the result can be encoded."""
    data = gzip.compress(_member_named(raw_name, b"hi", tar_format) + b"\0" * 1024)

    result = extract_files([("bundle.tgz", data)])

    assert result[0].text == f"--- {shown} ---\nhi"
    assert result[0].text.encode("utf-8") == b"--- " + shown.encode("utf-8") + b" ---\nhi"
    assert json.loads(result[0].model_dump_json())["text"] == f"--- {shown} ---\nhi"
    render_file_context(result).encode("utf-8")


@pytest.mark.parametrize(
    "tar_format", [tarfile.GNU_FORMAT, tarfile.PAX_FORMAT], ids=["gnu", "pax-record"]
)
def test_tgz_honest_non_ascii_member_name_is_unchanged(tar_format):
    name = "caf\u00e9/\u65e5\u672c\u8a9e-\u00f1-\U00010400.txt"
    data = gzip.compress(_member_named(name.encode(), b"hi", tar_format) + b"\0" * 1024)

    result = extract_files([("bundle.tgz", data)])

    assert result[0].text == f"--- {name} ---\nhi"
    assert result[0].text.encode("utf-8") == b"--- " + name.encode("utf-8") + b" ---\nhi"


def test_safe_member_name_replaces_every_lone_surrogate_and_nothing_else():
    honest = "caf\u00e9/\u65e5\u672c\u8a9e \ufffd\U00010400.txt"
    assert _safe_member_name(honest) == honest
    # The escapes tarfile produces, and the surrogates it does not, at both ends of
    # each range: none of them can be encoded, so none is left in.
    lone = "\ud800\udbff\udc00\udc80\udcff\udfff"
    assert _safe_member_name(f"a{lone}b") == "a" + "\ufffd" * 6 + "b"
    _safe_member_name(f"a{lone}b").encode("utf-8")
