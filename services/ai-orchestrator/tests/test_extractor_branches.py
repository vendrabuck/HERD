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
undecodable byte. Between them, a run of consecutive pax or long-name headers long
enough to make tarfile recurse past the interpreter's limit is an ExtractionError.

The last section pins what the reader holds in memory (issue #1201): the extended
headers in front of one member and the pax global headers of the archive have their own
ceilings, every sparse member is refused, the walk holds one member at a time, a few
hostile shapes are refused inside a 256 MiB address space, an error the walk does not
name is never turned into a refusal, and a text member cut inside a UTF-8 character
still decodes as UTF-8.
"""

import builtins
import gzip
import inspect
import io
import json
import pathlib
import struct
import subprocess
import sys
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

        def __getattr__(self, name):
            # next, members, and the rest of the walk's reads go to the real archive.
            return getattr(self._inner, name)

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
#
# Since #1201 the header ceilings (MAX_TGZ_MEMBER_HEADER_BYTES, 64 KiB, and
# MAX_TGZ_GLOBAL_HEADER_BYTES, 4 KiB) refuse such a header first, before the read bound
# is reached. The read bound stays as the second line, so the tests of it below raise
# the two header ceilings past it (the `header_ceilings_raised` fixture).


@pytest.fixture
def header_ceilings_raised(monkeypatch):
    """The #1201 header ceilings raised past the read bound, so a header reaches it."""
    monkeypatch.setattr(extractor, "MAX_TGZ_MEMBER_HEADER_BYTES", 64 * MAX_TAR_READ_BYTES)
    monkeypatch.setattr(extractor, "MAX_TGZ_GLOBAL_HEADER_BYTES", 64 * MAX_TAR_READ_BYTES)


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
def test_tgz_header_record_over_the_read_bound_is_refused_before_it_is_read(
    header_type, header_ceilings_raised
):
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


def test_tgz_pax_header_just_under_the_read_bound_extracts(header_ceilings_raised):
    result = extract_files([("bundle.tgz", _pax_tgz_with_comment(MAX_TAR_READ_BYTES - 4096))])
    assert result[0].text == "--- notes.txt ---\nhello"
    assert result[0].truncated is False


def test_tgz_well_formed_pax_header_just_over_the_read_bound_is_refused(
    header_ceilings_raised,
):
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
# A skipped member ending 3,584 bytes under the small ceiling, so a header record after
# it with 4 KiB of data, within its own #1201 ceiling, crosses the small one.
_FILLER_UNDER_THE_SMALL_CEILING = _header_block("a.bin", 61_440) + b"f" * 61_440
_RECORDS_PAST_A_SMALL_CEILING = {
    "skipped-member-data": _header_block("a.bin", 70_144) + b"s" * 70_144,
    "text-member-data": _header_block("a.txt", 70_144) + b"t" * 70_144,
    "many-small-members": _member_bytes("a.bin", b"m" * 512) * 80,
    "pax-extended-header": _FILLER_UNDER_THE_SMALL_CEILING
    + _header_block("././@PaxHeader", 4096, tarfile.XHDTYPE)
    + b"\0" * 4096
    + _header_block("a.bin"),
    "pax-global-header": _FILLER_UNDER_THE_SMALL_CEILING
    + _header_block("././@PaxHeader", 4096, tarfile.XGLTYPE)
    + b"\0" * 4096
    + _header_block("a.bin"),
    "gnu-long-name": _FILLER_UNDER_THE_SMALL_CEILING
    + _header_block("././@LongLink", 4096, tarfile.GNUTYPE_LONGNAME)
    + b"n" * 4095
    + b"\0"
    + _header_block("a.bin"),
    "gnu-long-link": _FILLER_UNDER_THE_SMALL_CEILING
    + _header_block("././@LongLink", 4096, tarfile.GNUTYPE_LONGLINK)
    + b"n" * 4095
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
    """Sixteen hundred pax extended headers of 64 KiB each, the most one member may
    carry (#1201), every one followed by an empty member: 101 MiB of stream in a small
    upload. Each header is within its ceiling, the members are far under the entry
    ceiling, and the member sizes sum to zero, so only a bound on the stream position
    sees them. Before #1201 the same flood with headers of just under 1 MiB, 4.7 GiB at
    the 5 MiB upload cap, took 9.7 s with no such bound."""
    header_bytes = extractor.MAX_TGZ_MEMBER_HEADER_BYTES
    pair = (
        _header_block("././@PaxHeader", header_bytes, tarfile.XHDTYPE)
        + b"\0" * header_bytes
        + _header_block("a.bin")
    )
    pairs = 1600
    assert pairs * len(pair) > extractor.MAX_TGZ_DECOMPRESSED_BYTES
    assert pairs < extractor.MAX_TGZ_ENTRIES
    data = _gzip_members((pair, pairs), (_NOTES_MEMBER_AND_END, 1))
    assert len(data) < 512 * 1024

    with pytest.raises(ExtractionError) as exc:
        extract_files([("bundle.tgz", data)])

    assert str(exc.value) == BYTES_REFUSAL
    # The whole pairs that fit are read; the next pair's header data would end past it.
    whole = extractor.MAX_TGZ_DECOMPRESSED_BYTES // len(pair)
    assert whole * len(pair) + 512 + header_bytes > extractor.MAX_TGZ_DECOMPRESSED_BYTES
    assert stream_reached == [whole * len(pair) + 512]
    assert stream_reached[0] <= extractor.MAX_TGZ_DECOMPRESSED_BYTES


# --- A run of consecutive headers that makes tarfile recurse ---
#
# tarfile reads a pax extended header or a GNU long name or long link by calling
# itself for the header that follows it, three frames a header on CPython 3.12. With
# the default limit of 1000 frames the smallest run that raised RecursionError was 328
# from a script and 327 from a worker thread. RecursionError is not a tarfile.TarError:
# before it was caught, a 1.8 KB upload of 400 such headers was an unhandled 500.

UNREADABLE_BY_RECURSION = "Failed to read tgz archive (RecursionError)"


def _small_header_record(name: str, type_: bytes, data: bytes) -> bytes:
    return _header_block(name, len(data), type_) + data.ljust(512, b"\0")


# Each kind of header tarfile reads that way, and the text an archive of such headers
# then notes.txt yields: a GNU long name renames the member, the others leave its name.
_CHAINED_HEADERS = {
    "pax-extended": (
        _small_header_record("././@PaxHeader", tarfile.XHDTYPE, b"13 comment=x\n"),
        _NOTES,
    ),
    "pax-global": (
        _small_header_record("././@PaxHeader", tarfile.XGLTYPE, b"13 comment=x\n"),
        _NOTES,
    ),
    "gnu-long-name": (
        _small_header_record("././@LongLink", tarfile.GNUTYPE_LONGNAME, b"long/name.txt\0"),
        "--- long/name.txt ---\nhello",
    ),
    "gnu-long-link": (
        _small_header_record("././@LongLink", tarfile.GNUTYPE_LONGLINK, b"long/link.txt\0"),
        _NOTES,
    ),
}

# Where the run starts decides where the error is raised: a run that opens the archive
# is read inside tarfile.open, a run after a member inside the iteration.
_BEFORE_THE_RUN = {"run-opens-the-archive": b"", "run-follows-a-member": _header_block("a.bin")}


@pytest.mark.parametrize("lead", _BEFORE_THE_RUN.values(), ids=_BEFORE_THE_RUN)
@pytest.mark.parametrize(
    "record", [record for record, _ in _CHAINED_HEADERS.values()], ids=_CHAINED_HEADERS
)
def test_tgz_run_of_consecutive_headers_that_recurses_is_an_extraction_error(
    record, lead, stream_reached, header_ceilings_raised
):
    """A run as long as the recursion limit itself: every header costs tarfile at least
    one frame, so the run passes the limit whatever the limit is. The error ends as
    the unreadable-archive ExtractionError naming its own class, with the gzip stream
    closed, and not as a RecursionError out of the extractor. The header ceilings are
    raised past the run so recursion, not a ceiling, is what stops it; the ceilings
    stopping such a run first is pinned below."""
    run = sys.getrecursionlimit()
    data = gzip.compress(lead + record * run + _NOTES_MEMBER_AND_END)
    assert len(data) < 8 * 1024

    with pytest.raises(ExtractionError) as exc:
        extract_files([("bundle.tgz", data)])

    assert str(exc.value) == UNREADABLE_BY_RECURSION
    assert type(exc.value.__cause__) is RecursionError
    assert len(stream_reached) == 1


@pytest.mark.parametrize("lead", _BEFORE_THE_RUN.values(), ids=_BEFORE_THE_RUN)
@pytest.mark.parametrize(("record", "text"), _CHAINED_HEADERS.values(), ids=_CHAINED_HEADERS)
def test_tgz_run_of_consecutive_headers_under_the_recursion_limit_extracts(
    record, text, lead, header_ceilings_raised
):
    """A hundred of the same headers in a row, a third of what recursed, read as they
    always did. The header ceilings are raised so this proves the recursion floor
    alone; a run of small headers reaching a ceiling first is pinned below."""
    data = gzip.compress(lead + record * 100 + _NOTES_MEMBER_AND_END)

    result = extract_files([("bundle.tgz", data)])

    assert result[0].text == text
    assert result[0].truncated is False


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


# --- What the reader holds in memory (issue #1201) ---
#
# The entry and byte ceilings bound how many members there are and how far the stream
# moves, not what tarfile builds from the bytes it reads. Before #1201 a 250 KB upload
# of one pax global header of just under 1 MiB (74,000 records, which tarfile copies
# into every later member) and 1,000 empty members peaked at 1.9 GiB resident, and a
# 20 KB upload of a GNU sparse 1.0 map of ten million numbers at 976 MiB; both were
# inside all three ceilings.

MEMBER_HEADER_REFUSAL = (
    "The tgz archive has more than 65536 bytes of pax extended headers and GNU long "
    "names in front of one member; nothing was extracted"
)
GLOBAL_HEADER_REFUSAL = (
    "The tgz archive has more than 4096 bytes of pax global headers; nothing was extracted"
)
SPARSE_REFUSAL = (
    "The tgz archive has a sparse member, which is not supported; nothing was extracted"
)

_MEMBER_HEADER_CEILING = 64 * 1024
_GLOBAL_HEADER_CEILING = 4 * 1024


def _padded(data: bytes) -> bytes:
    return data + b"\0" * ((512 - len(data) % 512) % 512)


def _record(name: str, type_: bytes, data: bytes) -> bytes:
    """A header of `type_` declaring len(data) bytes, then the data, padded to a block."""
    return _header_block(name, len(data), type_) + _padded(data)


def _pax_comment_of_length(total: int) -> bytes:
    """One pax record, a comment, of exactly `total` bytes."""
    for value_length in range(total - 20, total):
        record = _pax_record(b"comment", b"c" * value_length)
        if len(record) == total:
            return record
    raise AssertionError(f"no comment record of {total} bytes")


def _distinct_pax_records(total: int, prefix: bytes = b"k") -> bytes:
    """Pax records with distinct keywords and empty values, as many as fit in `total`
    bytes: the most dictionary entries tarfile can be made to build from that many."""
    records: list[bytes] = []
    size = 0
    for i in range(total):
        record = _pax_record(prefix + b"%07d" % i, b"")
        if size + len(record) > total:
            break
        records.append(record)
        size += len(record)
    return b"".join(records)


def _extended_header(type_: bytes, total: int) -> bytes:
    """An extended header of `type_` whose data is exactly `total` bytes: one pax
    comment record, or a GNU long name or link ending in `.txt`."""
    if type_ in (tarfile.XHDTYPE, tarfile.SOLARIS_XHDTYPE):
        return _record("././@PaxHeader", type_, _pax_comment_of_length(total))
    name = b"n" * (total - 5) + b".txt\0"
    return _record("././@LongLink", type_, name)


def _text_after(type_: bytes, total: int, member: str = "notes.txt") -> str:
    """The text the walk yields for `member` holding `hello` behind such a header: a
    long name renames the member, the other three leave its name."""
    if type_ == tarfile.GNUTYPE_LONGNAME:
        return "--- " + "n" * (total - 5) + ".txt ---\nhello"
    return f"--- {member} ---\nhello"


_EXTENDED_HEADER_TYPES = {
    "pax-x": tarfile.XHDTYPE,
    "solaris-X": tarfile.SOLARIS_XHDTYPE,
    "gnu-long-name": tarfile.GNUTYPE_LONGNAME,
    "gnu-long-link": tarfile.GNUTYPE_LONGLINK,
}


def test_tgz_header_ceilings_and_their_refusals_are_the_pinned_values():
    assert extractor.MAX_TGZ_MEMBER_HEADER_BYTES == _MEMBER_HEADER_CEILING
    assert extractor.MAX_TGZ_GLOBAL_HEADER_BYTES == _GLOBAL_HEADER_CEILING
    assert extractor.TGZ_MEMBER_HEADER_LIMIT_DETAIL == MEMBER_HEADER_REFUSAL
    assert extractor.TGZ_GLOBAL_HEADER_LIMIT_DETAIL == GLOBAL_HEADER_REFUSAL
    assert extractor.TGZ_SPARSE_DETAIL == SPARSE_REFUSAL
    # Both are under the read bound, so a header is refused by its own ceiling first.
    assert _GLOBAL_HEADER_CEILING < _MEMBER_HEADER_CEILING < MAX_TAR_READ_BYTES


@pytest.mark.parametrize("type_", _EXTENDED_HEADER_TYPES.values(), ids=_EXTENDED_HEADER_TYPES)
def test_tgz_extended_header_over_its_ceiling_is_refused_before_it_is_read(type_, stream_reached):
    data = gzip.compress(
        _extended_header(type_, _MEMBER_HEADER_CEILING + 1) + _NOTES_MEMBER_AND_END
    )

    with pytest.raises(ExtractionError) as exc:
        extract_files([("bundle.tgz", data)])

    assert str(exc.value) == MEMBER_HEADER_REFUSAL
    # Only the header's own 512-byte block was read, none of its data.
    assert stream_reached == [512]


@pytest.mark.parametrize("type_", _EXTENDED_HEADER_TYPES.values(), ids=_EXTENDED_HEADER_TYPES)
def test_tgz_extended_header_at_its_ceiling_extracts(type_):
    data = gzip.compress(_extended_header(type_, _MEMBER_HEADER_CEILING) + _NOTES_MEMBER_AND_END)

    result = extract_files([("bundle.tgz", data)])

    assert result[0].text == _text_after(type_, _MEMBER_HEADER_CEILING)
    assert result[0].truncated is False


# Two extended headers in a row in front of one member, each well under the ceiling.
_HEADER_RUNS = {
    "pax-then-long-name": (tarfile.XHDTYPE, tarfile.GNUTYPE_LONGNAME),
    "long-name-then-long-link": (tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK),
    "solaris-then-pax": (tarfile.SOLARIS_XHDTYPE, tarfile.XHDTYPE),
}


def _block(n: int) -> int:
    return -(-n // 512) * 512


@pytest.mark.parametrize(("first", "second"), _HEADER_RUNS.values(), ids=_HEADER_RUNS)
def test_tgz_run_of_extended_headers_is_counted_together(first, second, stream_reached):
    """tarfile holds every header of a run until the member is reached, so the run is
    what the ceiling counts, in the block-rounded bytes tarfile reads: a 40,448-byte
    header then a 30,208-byte one is refused at the second header's block, and the same
    first header then one filling the ceiling exactly (25,088 bytes) extracts."""
    first_total = 40_448
    lead = _extended_header(first, first_total)
    assert _block(first_total) == first_total

    over = gzip.compress(lead + _extended_header(second, 30_000) + _NOTES_MEMBER_AND_END)
    with pytest.raises(ExtractionError) as exc:
        extract_files([("bundle.tgz", over)])
    assert str(exc.value) == MEMBER_HEADER_REFUSAL
    assert stream_reached == [len(lead) + 512]

    rest = _MEMBER_HEADER_CEILING - first_total
    assert _block(rest) == rest
    at = gzip.compress(lead + _extended_header(second, rest) + _NOTES_MEMBER_AND_END)
    result = extract_files([("bundle.tgz", at)])
    # The first long name of a run names the member; tarfile applies it last.
    named_by = (first, first_total) if first == tarfile.GNUTYPE_LONGNAME else (second, rest)
    assert result[0].text == _text_after(*named_by)


def test_tgz_extended_header_count_starts_again_at_each_member():
    """Three members, each behind a pax header of the ceiling exactly: 192 KiB of
    extended headers in the archive, 64 KiB in front of any one member."""
    members = b"".join(
        _extended_header(tarfile.XHDTYPE, _MEMBER_HEADER_CEILING)
        + _member_bytes(f"m{i}.txt", b"hello")
        for i in range(3)
    )

    out = _extract_tgz(gzip.compress(members + b"\0" * 1024), remaining_budget=10_000)

    assert out == "\n\n".join(f"--- m{i}.txt ---\nhello" for i in range(3))


def _git_archive_global_header() -> bytes:
    """The pax global header git archive writes: one 52-byte comment naming the commit."""
    record = b"52 comment=" + b"0123456789abcdef0123456789abcdef01234567" + b"\n"
    assert len(record) == 52
    return _record("pax_global_header", tarfile.XGLTYPE, record)


def test_tgz_global_header_over_its_ceiling_is_refused_before_it_is_read(stream_reached):
    record = _pax_comment_of_length(_GLOBAL_HEADER_CEILING + 1)
    data = gzip.compress(
        _record("pax_global_header", tarfile.XGLTYPE, record) + _NOTES_MEMBER_AND_END
    )

    with pytest.raises(ExtractionError) as exc:
        extract_files([("bundle.tgz", data)])

    assert str(exc.value) == GLOBAL_HEADER_REFUSAL
    assert stream_reached == [512]


def test_tgz_global_headers_are_counted_across_the_archive(stream_reached):
    """tarfile keeps every global header's records for the rest of the walk, so the
    ceiling counts all of them: two of 2,500 bytes, one member apart, are refused at
    the second; one of the ceiling exactly extracts."""
    first = _record("pax_global_header", tarfile.XGLTYPE, _pax_comment_of_length(2500))
    between = _member_bytes("a.txt", b"first")
    second = _record("pax_global_header", tarfile.XGLTYPE, _pax_comment_of_length(2500))
    with pytest.raises(ExtractionError) as exc:
        _extract_tgz(
            gzip.compress(first + between + second + _NOTES_MEMBER_AND_END),
            remaining_budget=10_000,
        )
    assert str(exc.value) == GLOBAL_HEADER_REFUSAL
    assert stream_reached == [len(first) + len(between) + 512]

    exact = _record(
        "pax_global_header",
        tarfile.XGLTYPE,
        _pax_comment_of_length(_GLOBAL_HEADER_CEILING),
    )
    out = _extract_tgz(gzip.compress(exact + _NOTES_MEMBER_AND_END), remaining_budget=10_000)
    assert out == _NOTES


def test_tgz_from_git_archive_extracts_the_same_text_as_without_its_global_header():
    members = (
        _member_bytes("repo/README.md", b"# readme\n")
        + _member_bytes("repo/conf/router.cfg", b"hostname r1\n")
        + _member_bytes("repo/bin/tool", b"\x7fELF")
    )
    plain = _extract_tgz(gzip.compress(members + b"\0" * 1024), remaining_budget=10_000)

    with_header = _git_archive_global_header() + members + b"\0" * 1024
    out = _extract_tgz(gzip.compress(with_header), remaining_budget=10_000)

    assert out == plain
    assert (
        out == "--- repo/README.md ---\n# readme\n\n\n--- repo/conf/router.cfg ---\nhostname r1\n"
    )


def test_tgz_pax_archive_with_long_utf8_paths_extracts():
    """tarfile's own PAX writer: each long or non-ASCII path goes in a pax path record."""
    buf = io.BytesIO()
    names = ["données/" + "réseau-üñïçødé-日本/" * 12 + f"conf{i}.txt" for i in range(20)]
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
        for i, name in enumerate(names):
            body = f"config {i} café\n".encode()
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    assert all(len(name.encode()) > 255 for name in names)

    out = _extract_tgz(buf.getvalue(), remaining_budget=100_000)

    assert out == "\n\n".join(f"--- {name} ---\nconfig {i} café\n" for i, name in enumerate(names))


def test_tgz_gnu_archive_of_the_entry_ceiling_with_long_names_extracts():
    """tarfile's own GNU writer, 10,000 members, every name long enough to need a GNU
    long name header in front of its member."""
    buf = io.BytesIO()
    prefix = "support-bundle/" + "d" * 100 + "/"
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.GNU_FORMAT) as tar:
        for i in range(extractor.MAX_TGZ_ENTRIES - 1):
            tar.addfile(tarfile.TarInfo(f"{prefix}f{i}.bin"))
        info = tarfile.TarInfo(f"{prefix}readme.txt")
        info.size = 2
        tar.addfile(info, io.BytesIO(b"hi"))

    assert _extract_tgz(buf.getvalue(), remaining_budget=10_000) == (
        f"--- {prefix}readme.txt ---\nhi"
    )


def test_tgz_three_megabyte_utf8_text_member_extracts_whole(monkeypatch):
    content = "é".encode() * 1_500_000
    assert len(content) == 3_000_000
    monkeypatch.setattr(config_module.settings, "upload_max_extracted_chars", 4_000_000)

    result = extract_files([("bundle.tgz", _make_tgz([("big.log", content)]))])

    assert result[0].text == "--- big.log ---\n" + "é" * 1_500_000
    assert result[0].truncated is False


# --- Sparse members ---


def _old_gnu_sparse_member(extension_blocks: int) -> bytes:
    """A GNU type S member whose header says extension blocks of sparse map follow, and
    that many blocks, each holding 21 map entries, the last one saying no more follow."""
    header = bytearray(_header_block("s.bin", 0, tarfile.GNUTYPE_SPARSE))
    header[482] = 1  # isextended
    header[148:156] = b" " * 8
    header[148:156] = b"%06o\0 " % sum(header)
    block = bytearray(512)
    for i in range(21):
        block[i * 24 : i * 24 + 24] = b"%011o\0%011o\0" % (i + 1, 1)
    more = bytes(block[:504]) + b"\1" + bytes(7)
    last = bytes(block[:504]) + b"\0" + bytes(7)
    return bytes(header) + more * (extension_blocks - 1) + last


def _pax_sparse(records: bytes, map_data: bytes = b"") -> bytes:
    """A pax extended header of sparse records, then the member it describes."""
    return (
        _record("././@PaxHeader", tarfile.XHDTYPE, records)
        + _header_block("s.bin", len(_padded(map_data)))
        + _padded(map_data)
    )


_SPARSE_10_RECORDS = (
    _pax_record(b"GNU.sparse.major", b"1")
    + _pax_record(b"GNU.sparse.minor", b"0")
    + _pax_record(b"GNU.sparse.name", b"s.txt")
    + _pax_record(b"GNU.sparse.realsize", b"10")
)

# Each GNU sparse format, and the stream position at which its refusal comes: the old
# type S member before its first extension block; the three pax formats once the pax
# header and the member's own header are read, before any member data (where the 1.0
# map is).
_SPARSE_MEMBERS = {
    "gnu-type-S": (_old_gnu_sparse_member(3), 512),
    "pax-0.0": (
        _pax_sparse(
            _pax_record(b"GNU.sparse.size", b"10")
            + _pax_record(b"GNU.sparse.offset", b"0")
            + _pax_record(b"GNU.sparse.numbytes", b"1")
        ),
        1536,
    ),
    "pax-0.1": (
        _pax_sparse(
            _pax_record(b"GNU.sparse.size", b"10") + _pax_record(b"GNU.sparse.map", b"0,1,4,1")
        ),
        1536,
    ),
    "pax-1.0": (_pax_sparse(_SPARSE_10_RECORDS, b"2\n0\n1\n4\n1\n"), 1536),
}


@pytest.mark.parametrize(("member", "reached"), _SPARSE_MEMBERS.values(), ids=_SPARSE_MEMBERS)
def test_tgz_sparse_member_is_refused_before_its_map_is_read(member, reached, stream_reached):
    data = gzip.compress(member + _NOTES_MEMBER_AND_END)

    with pytest.raises(ExtractionError) as exc:
        extract_files([("bundle.tgz", data)])

    assert str(exc.value) == SPARSE_REFUSAL
    assert stream_reached == [reached]


@pytest.mark.parametrize(("member", "reached"), _SPARSE_MEMBERS.values(), ids=_SPARSE_MEMBERS)
def test_tgz_sparse_member_is_refused_wherever_it_is(member, reached):
    """After a text member as well as first: the file is refused and no text is kept."""
    data = gzip.compress(_member_bytes("a.txt", b"kept?") + member + _NOTES_MEMBER_AND_END)

    with pytest.raises(ExtractionError) as exc:
        _extract_tgz(data, remaining_budget=10_000)

    assert str(exc.value) == SPARSE_REFUSAL


def test_tarfile_still_has_the_methods_the_bounded_tarinfo_overrides():
    """_BoundedTarInfo overrides private tarfile methods. If a Python upgrade renames
    one or changes how it is called, the override would stop being called and the
    refusal it carries would be gone without any other test noticing first."""
    overridden = {
        "_proc_member": ["archive"],
        "_apply_pax_info": ["pax_headers", "encoding", "errors"],
        "_proc_gnusparse_00": ["next_info", "raw_headers"],
        "_proc_gnusparse_01": ["next_info", "pax_headers"],
        "_proc_gnusparse_10": ["next_info", "pax_headers", "archive"],
    }
    for name, parameters in overridden.items():
        base = vars(tarfile.TarInfo).get(name)
        assert callable(base), f"tarfile.TarInfo.{name} is gone"
        assert len(inspect.signature(base).parameters) == len(parameters) + 1, name
        ours = vars(extractor._BoundedTarInfo)[name]
        assert list(inspect.signature(ours).parameters)[1:] == parameters
    # tarfile builds every header through the class it is opened with, and hands the
    # three pax sparse formats over from _proc_pax, to the methods overridden above.
    assert "tarinfo" in inspect.signature(tarfile.TarFile.__init__).parameters
    pax_source = inspect.getsource(tarfile.TarInfo._proc_pax)
    for name in ("_proc_gnusparse_00", "_proc_gnusparse_01", "_proc_gnusparse_10"):
        assert f"self.{name}(" in pax_source, name
    # _proc_pax applies the records through _apply_pax_info, the ValueError seam #1201
    # guards; both the extended and the global path reach it.
    assert "_apply_pax_info(" in pax_source
    # _proc_member and _proc_gnulong read self._block(self.size), the byte count the
    # ceilings use; a rename there would uncount a padded header's records.
    assert "self._block(self.size)" in inspect.getsource(tarfile.TarInfo._proc_pax)
    assert "self._block(self.size)" in inspect.getsource(tarfile.TarInfo._proc_gnulong)
    # TarFile.next keeps what it reads in TarFile.members, which the walk empties.
    assert "self.members.append(tarinfo)" in inspect.getsource(tarfile.TarFile.next)


def test_tgz_walk_holds_one_member_at_a_time(monkeypatch):
    """tarfile appends every member it reads to TarFile.members. The walk empties the
    list after each step, so the members it has passed are not kept."""
    held: list[int] = []
    real_next = tarfile.TarFile.next

    def recording_next(self):
        held.append(len(self.members))
        return real_next(self)

    monkeypatch.setattr(tarfile.TarFile, "next", recording_next)
    data = _gzip_members((_header_block("a.bin"), 500), (_NOTES_MEMBER_AND_END, 1))

    assert _extract_tgz(data, remaining_budget=10_000) == _NOTES
    assert len(held) > 500
    # tarfile.open reads the first member, which the walk's first step returns.
    assert max(held) <= 1


# --- Errors the walk does not name propagate unchanged ---


@pytest.mark.parametrize(
    "error", [KeyError("injected"), MemoryError()], ids=["KeyError", "MemoryError"]
)
@pytest.mark.parametrize("where", ["in-the-walk", "inside-tarfile"])
def test_tgz_walk_lets_an_error_it_does_not_name_propagate(error, where, monkeypatch):
    """Only gzip failures, tar errors, RecursionError, and the extractor's own refusals
    become an ExtractionError. A programming error or a MemoryError is not an archive
    the reader cannot read, and must not answer as one."""

    def raise_it(*args, **kwargs):
        raise error

    if where == "in-the-walk":
        monkeypatch.setattr(extractor, "_safe_member_name", raise_it)
    else:
        monkeypatch.setattr(extractor._BoundedTarInfo, "_proc_member", raise_it)

    with pytest.raises(type(error)) as exc:
        _extract_tgz(_make_tgz([("a.txt", b"hello")]), remaining_budget=10_000)

    assert exc.value is error


# --- A bad tar header after the first member ends the archive ---


def _member_with_a_bad_checksum(name: str, content: bytes) -> bytes:
    member = bytearray(_member_bytes(name, content))
    member[148:156] = b"1234567\0"
    return bytes(member)


_BAD_HEADERS_AFTER_A_MEMBER = {
    "bad-checksum": _member_with_a_bad_checksum("b.txt", b"second")
    + _member_bytes("c.txt", b"third")
    + b"\0" * 1024,
    "cut-inside-the-header": _member_bytes("b.txt", b"second")[:300],
}


@pytest.mark.parametrize(
    "tail", _BAD_HEADERS_AFTER_A_MEMBER.values(), ids=_BAD_HEADERS_AFTER_A_MEMBER
)
def test_tgz_bad_header_after_the_first_member_yields_the_members_before_it(tail):
    """Pinned as is, by decision: tarfile reads an invalid or cut header after the first
    member as the end of the archive, so a gzip stream that is whole but whose tar is
    bad after its first member extracts the members before it. The same bad header
    first is an unreadable archive."""
    first = _member_bytes("a.txt", b"first")

    assert _extract_tgz(gzip.compress(first + tail), remaining_budget=10_000) == (
        "--- a.txt ---\nfirst"
    )
    with pytest.raises(ExtractionError) as exc:
        _extract_tgz(gzip.compress(tail + first), remaining_budget=10_000)
    assert str(exc.value) == UNREADABLE_TGZ


# A bad header ends the archive only where a member's own header should be. After a pax
# extended, global, or GNU long-name header, tarfile expects the described member's
# header and raises SubsequentHeaderError, which TarFile.next turns into ReadError at
# any offset, so the file is the 400 unreadable archive (issue #1201 review finding 2).
_EXTENDED_BEFORE_A_BAD_HEADER = {
    "pax-extended": _record("././@PaxHeader", tarfile.XHDTYPE, b"13 comment=x\n"),
    "pax-global": _record("pax_global_header", tarfile.XGLTYPE, b"13 comment=x\n"),
    "gnu-long-name": _record("././@LongLink", tarfile.GNUTYPE_LONGNAME, b"later.txt\0"),
}


@pytest.mark.parametrize(
    "extended", _EXTENDED_BEFORE_A_BAD_HEADER.values(), ids=_EXTENDED_BEFORE_A_BAD_HEADER
)
def test_tgz_bad_header_after_an_extended_header_is_an_unreadable_archive(extended):
    first = _member_bytes("a.txt", b"first")
    bad = _member_with_a_bad_checksum("b.txt", b"second")
    data = gzip.compress(first + extended + bad + _member_bytes("c.txt", b"third") + b"\0" * 1024)

    with pytest.raises(ExtractionError) as exc:
        _extract_tgz(data, remaining_budget=10_000)

    assert str(exc.value) == UNREADABLE_TGZ


# --- The ceilings count the block-rounded bytes tarfile reads, not the declared size ---
#
# tarfile's _proc_pax reads self._block(self.size) bytes and parses records until the
# block ends or a NUL, so a header declaring 1 byte can carry up to 511 bytes of
# records. Counting the declared size let a flood of such headers pass every ceiling and
# drive the reader to gigabytes (issue #1201 review finding 1).


def _padded_header(type_: bytes, name: str, records: bytes) -> bytes:
    """A header declaring size 1 whose 512-byte block is filled with `records`."""
    assert 0 < len(records) <= 511
    return _header_block(name, 1, type_) + records.ljust(512, b"\0")


def _distinct_records_for_a_block(prefix: bytes) -> bytes:
    """As many distinct empty-valued pax records as fit in one 512-byte block."""
    records: list[bytes] = []
    size = 0
    i = 0
    while True:
        record = _pax_record(prefix + b"%04d" % i, b"")
        if size + len(record) > 511:
            break
        records.append(record)
        size += len(record)
        i += 1
    return b"".join(records)


def test_tgz_padded_global_headers_are_counted_by_their_block(stream_reached):
    """Nine global headers, each declaring 1 byte but carrying a full block of distinct
    records: 9 blocks is 4,608 bytes, past the 4 KiB ceiling, so the ninth is refused.
    Declared-size counting would see 9 bytes and let them all through."""
    headers = [
        _padded_header(
            tarfile.XGLTYPE, "pax_global_header", _distinct_records_for_a_block(b"g%d_" % i)
        )
        for i in range(9)
    ]
    data = gzip.compress(b"".join(headers) + _NOTES_MEMBER_AND_END)
    assert len(data) < 64 * 1024

    with pytest.raises(ExtractionError) as exc:
        extract_files([("bundle.tgz", data)])

    assert str(exc.value) == GLOBAL_HEADER_REFUSAL
    # Eight headers' data blocks fit (4,096 counted bytes); the ninth is refused at its
    # own 512-byte descriptor, before its data block. Each header spans a descriptor and
    # a data block on the stream, so the position is eight headers plus the ninth's
    # descriptor.
    assert stream_reached == [8 * 1024 + 512]


def test_tgz_padded_member_headers_are_counted_by_their_block(stream_reached):
    """A run of 129 extended headers in front of one member, each declaring 1 byte but
    carrying a full block: 129 blocks is 66,048 bytes, past the 64 KiB ceiling. 128
    blocks is the ceiling exactly and extracts."""
    one = _padded_header(tarfile.XHDTYPE, "././@PaxHeader", _distinct_records_for_a_block(b"m_"))
    over = gzip.compress(one * 129 + _NOTES_MEMBER_AND_END)
    with pytest.raises(ExtractionError) as exc:
        extract_files([("bundle.tgz", over)])
    assert str(exc.value) == MEMBER_HEADER_REFUSAL
    assert stream_reached == [128 * 1024 + 512]

    at = gzip.compress(one * 128 + _NOTES_MEMBER_AND_END)
    assert extract_files([("bundle.tgz", at)])[0].text == _NOTES


def test_tgz_run_of_small_headers_reaches_the_ceiling_before_tarfile_recurses(stream_reached):
    """With block counting, a run of minimal headers hits the member ceiling at the
    129th, well before the recursion limit, so the RecursionError catch is a genuine
    second line. The same run with the ceiling raised recurses (pinned above)."""
    record = _small_header_record("././@PaxHeader", tarfile.XHDTYPE, b"13 comment=x\n")
    run = sys.getrecursionlimit()
    assert run > 129
    data = gzip.compress(record * run + _NOTES_MEMBER_AND_END)

    with pytest.raises(ExtractionError) as exc:
        extract_files([("bundle.tgz", data)])

    assert str(exc.value) == MEMBER_HEADER_REFUSAL
    assert stream_reached == [128 * 1024 + 512]


# --- A text member cut inside a UTF-8 character ---


def test_tgz_member_cut_inside_a_utf8_character_decodes_as_utf8():
    """The review's shape: an `a` and then e-acute, cut by the default budget inside a
    character. Before #1201 the whole member came back as Latin-1 mojibake."""
    content = b"a" + "é".encode() * 100_000
    budget = 80_000
    cut = budget + 1024
    assert (cut - 1) % 2 == 1  # the cut is inside a two-byte character

    out = _extract_tgz(_make_tgz([("u.txt", content)]), remaining_budget=budget)

    assert out == "--- u.txt ---\na" + "é" * ((cut - 1) // 2)


_MULTIBYTE = {"2-byte": "é", "3-byte": "日", "4-byte": "\U0001f600"}


@pytest.mark.parametrize("char", _MULTIBYTE.values(), ids=_MULTIBYTE)
def test_tgz_member_cut_at_every_byte_of_a_character_keeps_the_whole_characters(char):
    encoded = char.encode()
    budget = 1000
    cut = budget + 1024
    for inside in range(len(encoded)):
        lead = (cut - inside) % len(encoded)
        content = b"a" * lead + encoded * 2000
        assert (cut - lead) % len(encoded) == inside

        out = _extract_tgz(_make_tgz([("u.txt", content)]), remaining_budget=budget)

        assert out == "--- u.txt ---\n" + "a" * lead + char * ((cut - lead) // len(encoded))


def test_tgz_cut_member_that_is_not_utf8_still_falls_back_to_latin1():
    content = b"\xe9" * 5000
    out = _extract_tgz(_make_tgz([("l.txt", content)]), remaining_budget=1000)
    assert out == "--- l.txt ---\n" + "é" * 2024


def test_tgz_cut_ending_in_a_cesu8_surrogate_falls_back_to_latin1():
    """A cut whose held-back tail can never start a UTF-8 character: the incremental
    decoder BUFFERS a CESU-8 surrogate lead pair (ED A0) rather than raising, so the
    whole-character path would drop it silently. The member falls back to Latin-1
    instead, so those bytes are not lost (issue #1201 review finding 5)."""
    budget = 1000
    cut = budget + 1024
    content = b"a" * (cut - 2) + b"\xed\xa0" + b"a" * 2000
    assert content[:cut].endswith(b"\xed\xa0") and len(content) > cut

    out = _extract_tgz(_make_tgz([("c.txt", content)]), remaining_budget=budget)

    assert out == "--- c.txt ---\n" + content[:cut].decode("latin-1")
    # Not the silent-drop path, which would lose the two surrogate bytes.
    assert out != "--- c.txt ---\n" + "a" * (cut - 2)


def test_tgz_whole_member_ending_inside_a_character_is_read_as_latin1():
    """Only a cut read holds back an incomplete character; a member read whole decodes
    as before, so a broken last character still makes the whole member Latin-1."""
    content = "é".encode() * 10 + b"\xc3"
    out = _extract_tgz(_make_tgz([("w.txt", content)]), remaining_budget=10_000)
    assert out == "--- w.txt ---\n" + content.decode("latin-1")


# --- Peak memory, measured in a child process under a 256 MiB address space ---
#
# Each hostile archive is walked by _extract_tgz in a fresh Python process whose address
# space is capped with RLIMIT_AS after the import. The child reports the outcome, the
# walk's wall time, and its resident peak before and after the walk. The peak is the
# process's VmHWM, reset to the current resident size just before the walk by writing 5
# to /proc/self/clear_refs. ru_maxrss would not do: Linux carries the parent's peak
# across exec, so in a child of a large pytest process it reads the parent's size and a
# walk smaller than that shows no growth at all.

_ADDRESS_SPACE_CAP = 256 * _MIB

_MEMORY_PROBE = """
import json, resource, sys, time
from app.services import extractor

def resident_peak_kib():
    with open("/proc/self/status") as status:
        for line in status:
            if line.startswith("VmHWM:"):
                return int(line.split()[1])
    raise RuntimeError("no VmHWM")

data = open(sys.argv[1], "rb").read()
cap = int(sys.argv[2])
with open("/proc/self/clear_refs", "w") as clear_refs:
    clear_refs.write("5")
before = resident_peak_kib()
resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
start = time.monotonic()
try:
    extractor._extract_tgz(data, 80_000)
    outcome = "extracted"
except extractor.ExtractionError as e:
    outcome = str(e)
except MemoryError:
    outcome = "MemoryError"
seconds = time.monotonic() - start
peak = resident_peak_kib()
print(json.dumps({"outcome": outcome, "seconds": seconds, "before_kib": before, "peak_kib": peak}))
"""


def _walk_in_a_capped_child(data: bytes, tmp_path) -> dict:
    path = tmp_path / "upload.tgz"
    path.write_bytes(data)
    service_dir = pathlib.Path(__file__).resolve().parents[1]
    done = subprocess.run(
        [sys.executable, "-c", _MEMORY_PROBE, str(path), str(_ADDRESS_SPACE_CAP)],
        cwd=service_dir,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    return json.loads(done.stdout.strip().splitlines()[-1])


_needs_rlimit = pytest.mark.skipif(
    sys.platform != "linux", reason="RLIMIT_AS and /proc/self/status are Linux behavior"
)


@_needs_rlimit
def test_tgz_padded_global_header_flood_is_refused_in_bounded_memory(tmp_path):
    """The review's 1.7 GiB shape (finding 1): 4,096 global headers each DECLARING one
    byte but carrying a full block of distinct records, then a run of 230 zero-size pax
    extended headers in front of one member. Every header passes the declared-size
    count; the block count refuses at the ninth global header. Before the fix this
    reached 1.7 GiB at an 8 GiB cap and a MemoryError 500 at 256 MiB."""
    globals_ = [
        _padded_header(
            tarfile.XGLTYPE, "pax_global_header", _distinct_records_for_a_block(b"g%d_" % (i % 97))
        )
        + _header_block("a.bin")
        for i in range(4096)
    ]
    run = _header_block("././@PaxHeader", 0, tarfile.XHDTYPE) * 230
    data = gzip.compress(b"".join(globals_) + run + _member_bytes("b.txt", b"hello") + b"\0" * 1024)
    assert len(data) < 700 * 1024

    report = _walk_in_a_capped_child(data, tmp_path)

    assert report["outcome"] == GLOBAL_HEADER_REFUSAL
    assert report["seconds"] < 2
    assert report["peak_kib"] - report["before_kib"] < 16 * 1024


@_needs_rlimit
def test_tgz_global_header_copied_into_a_thousand_members_is_refused_in_bounded_memory(
    tmp_path,
):
    """The review's 1.9 GiB shape: a global header of just under 1 MiB of distinct
    records, then 1,000 empty members, in a 250 KB upload."""
    records = _distinct_pax_records(_MIB - 4096)
    data = _gzip_members(
        (_record("pax_global_header", tarfile.XGLTYPE, records), 1),
        (_header_block("a.bin"), 1000),
        (b"\0" * 1024, 1),
    )
    assert len(data) < 300 * 1024

    report = _walk_in_a_capped_child(data, tmp_path)

    assert report["outcome"] == GLOBAL_HEADER_REFUSAL
    assert report["seconds"] < 2
    assert report["peak_kib"] - report["before_kib"] < 16 * 1024


@_needs_rlimit
def test_tgz_sparse_map_of_ten_million_numbers_is_refused_in_bounded_memory(tmp_path):
    """The review's 976 MiB shape: a GNU sparse 1.0 map of ten million numbers, 20 MB
    of stream in a 20 KB upload."""
    numbers = 10_000_000
    chunk = b"0\n" * (512 * 1024)
    map_data = b"%d\n" % (numbers // 2) + chunk * (2 * numbers // len(chunk))
    map_data = _padded(map_data)
    data = _gzip_members(
        (_record("././@PaxHeader", tarfile.XHDTYPE, _SPARSE_10_RECORDS), 1),
        (_header_block("s.bin", len(map_data)) + map_data[:1024], 1),
        (chunk, (len(map_data) - 1024) // len(chunk)),
        (map_data[1024 + ((len(map_data) - 1024) // len(chunk)) * len(chunk) :], 1),
        (b"\0" * 1024, 1),
    )
    assert len(data) < 64 * 1024

    report = _walk_in_a_capped_child(data, tmp_path)

    assert report["outcome"] == SPARSE_REFUSAL
    assert report["seconds"] < 2
    assert report["peak_kib"] - report["before_kib"] < 16 * 1024


@_needs_rlimit
def test_tgz_members_each_behind_a_full_extended_header_walk_in_bounded_memory(tmp_path):
    """A hundred and fifty members, each behind a pax header of the ceiling exactly,
    filled with distinct records: inside every ceiling, so the archive extracts. Each
    member's records are about 0.3 MiB once tarfile has parsed them; kept for every
    member, as TarFile.members keeps them, they were about 50 MiB here."""
    pair = _record(
        "././@PaxHeader", tarfile.XHDTYPE, _distinct_pax_records(_MEMBER_HEADER_CEILING)
    ) + _header_block("a.bin")
    data = _gzip_members((pair, 150), (b"\0" * 1024, 1))

    report = _walk_in_a_capped_child(data, tmp_path)

    assert report["outcome"] == "extracted"
    assert report["peak_kib"] - report["before_kib"] < 16 * 1024
