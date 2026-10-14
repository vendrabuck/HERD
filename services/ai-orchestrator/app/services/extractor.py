"""Text extraction for user-uploaded context files.

Supported types: .pdf, .txt, .md, .json, .xml, .tgz (tech support bundles).
No persistent storage: files are read into memory, extracted, and discarded.
Per-file and aggregate size caps defend against oversized uploads and
runaway token costs.
"""

import gzip
import io
import json
import logging
import re
import tarfile
import zlib
from dataclasses import dataclass
from typing import IO

from herd_common.bounded_tar import TAR_READ_LIMIT_TEXT, BoundedTarStream, TarReadLimitError

from app.config import settings
from app.schemas.generate import ExtractedFile

logger = logging.getLogger(__name__)


SUPPORTED_EXTENSIONS = (".pdf", ".txt", ".md", ".json", ".xml", ".tgz", ".tar.gz")
TEXT_EXTENSIONS = (".txt", ".md", ".json", ".xml", ".log", ".cfg", ".conf", ".yaml", ".yml")

# The refusal for a tgz holding a tar header record over herd_common's
# MAX_TAR_READ_BYTES (issue #1192).
TGZ_READ_LIMIT_DETAIL = f"The tgz archive has a {TAR_READ_LIMIT_TEXT}; nothing was extracted"

# Ceilings on one uploaded tgz (issue #1195), the driver loader's values
# (MAX_ARCHIVE_ENTRIES and MAX_EXTRACTED_BYTES in execution's driver_loader.py). They
# are fixed, not settings. Nothing else stops the walk of a hostile archive: the
# character budget advances only on text members, and a tar of empty members or of
# zeros compresses several hundred to one, so an upload under the per-file cap can
# hold millions of members or gigabytes of data to pass over.
MAX_TGZ_ENTRIES = 10_000
MAX_TGZ_DECOMPRESSED_BYTES = 100 * 1024 * 1024

TGZ_ENTRY_LIMIT_DETAIL = (
    f"The tgz archive has more than {MAX_TGZ_ENTRIES} entries; nothing was extracted"
)
TGZ_BYTES_LIMIT_DETAIL = (
    f"The tgz archive extends past {MAX_TGZ_DECOMPRESSED_BYTES} bytes uncompressed; "
    "nothing was extracted"
)

# The largest single read of one tgz member's data. The tar is read through
# BoundedTarStream, which refuses any single read over MAX_TAR_READ_BYTES, and one
# read of a member reaches the stream as one read of up to that size; a text member
# may be as large as upload_max_file_bytes, so it is read in chunks of this size. It
# must not exceed MAX_TAR_READ_BYTES.
_MEMBER_READ_CHUNK_BYTES = 64 * 1024

# What the gzip layer raises for a body that is not gzip (BadGzipFile, an OSError),
# ends early (EOFError), holds corrupt compressed data (zlib.error), or fails its
# checksum (BadGzipFile). It reads lazily, so any of these can come from any read.
_GZIP_FAILURES = (OSError, EOFError, zlib.error)

# A lone surrogate: what tarfile leaves in a member name for each byte it could not
# decode (it decodes names with the surrogateescape error handler).
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


class ExtractionError(Exception):
    """Raised for invalid, oversized, or unparsable files."""


class _TgzLimitError(Exception):
    """An uploaded tgz passed a ceiling; the message is the refusal's detail."""


class _CeilingStream:
    """The decompressed tgz stream, refusing to move past MAX_TGZ_DECOMPRESSED_BYTES.

    Every byte tarfile takes from the archive comes through ``read`` or is passed over
    by ``seek``: member data that is read, the data of a skipped member (tarfile skips
    it by seeking forward, which decompresses and discards it), pax extended headers
    and GNU long names (read inside tarfile and never yielded as members, so no sum of
    member sizes counts them), and padding. Bounding the position bounds all of them,
    whatever a header declares. A read or a seek that would end past the ceiling is
    refused before the stream moves, so the bytes past it are never decompressed.

    It sits inside ``BoundedTarStream``, which has already refused an unbounded read,
    so ``read`` is always given a real size.
    """

    def __init__(self, raw: gzip.GzipFile) -> None:
        self._raw = raw

    def _refuse_past_ceiling(self, end: int) -> None:
        if end > MAX_TGZ_DECOMPRESSED_BYTES:
            raise _TgzLimitError(TGZ_BYTES_LIMIT_DETAIL)

    def read(self, size: int) -> bytes:
        self._refuse_past_ceiling(self._raw.tell() + size)
        return self._raw.read(size)

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        # tarfile seeks from the start only. Any other seek is refused, as an OSError
        # the caller reads as an unreadable archive, so no seek goes unchecked.
        if whence != io.SEEK_SET:
            raise io.UnsupportedOperation("the tgz stream seeks from the start only")
        self._refuse_past_ceiling(offset)
        return self._raw.seek(offset)

    def tell(self) -> int:
        return self._raw.tell()


@dataclass
class _Raw:
    filename: str
    data: bytes


def _extension(filename: str) -> str:
    lower = filename.lower()
    if lower.endswith(".tar.gz"):
        return ".tgz"
    dot = lower.rfind(".")
    return lower[dot:] if dot >= 0 else ""


def _decode_text(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1", errors="replace")


def _safe_member_name(name: str) -> str:
    """A tgz member's name with each undecodable byte shown as U+FFFD (issue #1196).

    tarfile turns a name byte that is not valid UTF-8 into a lone surrogate, which
    cannot be encoded as UTF-8, so the extracted text could not be sent to a provider
    or serialized. A name that decoded cleanly holds no surrogate and is returned as
    it is.
    """
    return _LONE_SURROGATE.sub("\ufffd", name)


def _extract_pdf(data: bytes) -> str:
    try:
        import pdfplumber
    except ImportError as e:
        raise ExtractionError("PDF extraction not available: pdfplumber is not installed") from e

    parts: list[str] = []
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                if text:
                    parts.append(text)
    except Exception as e:
        # Issue #1036: the parser's own text stays out of the 400 detail.
        raise ExtractionError(f"Failed to parse PDF ({type(e).__name__})") from e
    return "\n\n".join(parts)


def _read_member(f: IO[bytes], want: int) -> bytes:
    """Read up to `want` bytes of one tgz member, _MEMBER_READ_CHUNK_BYTES at a time."""
    chunks: list[bytes] = []
    got = 0
    while got < want:
        chunk = f.read(min(_MEMBER_READ_CHUNK_BYTES, want - got))
        if not chunk:
            break
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


def _extract_tgz(data: bytes, remaining_budget: int) -> str:
    """Extract text members from a gzipped tar, stopping when the budget runs out.

    The tar is read through ``herd_common.bounded_tar.BoundedTarStream``, so no single
    read from the decompressed stream exceeds MAX_TAR_READ_BYTES: a pax or GNU
    long-name header declaring more is refused with TGZ_READ_LIMIT_DETAIL before it
    is read into memory (issue #1192). Opening with ``tarfile.open(mode="r:gz")``
    instead reads through tarfile's own gzip stream, which that bound cannot see.
    Every failure of the gzip layer is raised as ``tarfile.ReadError``, the class
    tarfile's gzip mode gives a body that is not gzip, so it ends as an
    ExtractionError like any other unreadable archive.

    The walk is bounded (issue #1195). Every member the iteration yields is counted,
    whatever its type or extension, and the member after MAX_TGZ_ENTRIES is refused
    with TGZ_ENTRY_LIMIT_DETAIL before another header is read, which bounds what
    tarfile keeps in ``TarFile.members``. ``_CeilingStream`` refuses, with
    TGZ_BYTES_LIMIT_DETAIL, any read or seek that would move the decompressed stream
    past MAX_TGZ_DECOMPRESSED_BYTES. Either refusal fails the file; no text is kept.

    tarfile reads a pax extended header or a GNU long name by calling itself for the
    header that follows, so a run of a few hundred of them in a row, a kilobyte or two
    compressed, ends in ``RecursionError``. That is not a ``tarfile.TarError``; it is
    caught here by name and ends as the same ExtractionError as any other archive
    that cannot be read, naming its own class.
    """
    parts: list[str] = []
    used = 0
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data), mode="rb") as gz:
            try:
                stream = BoundedTarStream(_CeilingStream(gz))
                with tarfile.open(fileobj=stream, mode="r:") as tar:
                    for entries, member in enumerate(tar, start=1):
                        if entries > MAX_TGZ_ENTRIES:
                            raise _TgzLimitError(TGZ_ENTRY_LIMIT_DETAIL)
                        if not member.isfile():
                            continue
                        if _extension(member.name) not in TEXT_EXTENSIONS:
                            continue
                        if member.size <= 0 or member.size > settings.upload_max_file_bytes:
                            continue
                        if used >= remaining_budget:
                            break
                        f = tar.extractfile(member)
                        if f is None:
                            continue
                        raw = _read_member(f, min(member.size, remaining_budget - used + 1024))
                        text = _decode_text(raw)
                        header = f"--- {_safe_member_name(member.name)} ---\n"
                        chunk = header + text
                        parts.append(chunk)
                        used += len(chunk)
            except _GZIP_FAILURES as e:
                raise tarfile.ReadError("not a readable gzip stream") from e
    except TarReadLimitError as e:
        raise ExtractionError(TGZ_READ_LIMIT_DETAIL) from e
    except _TgzLimitError as e:
        raise ExtractionError(str(e)) from e
    except (tarfile.TarError, RecursionError) as e:
        raise ExtractionError(f"Failed to read tgz archive ({type(e).__name__})") from e
    return "\n\n".join(parts)


def _extract_one(raw: _Raw, remaining_budget: int) -> str:
    ext = _extension(raw.filename)
    if ext not in SUPPORTED_EXTENSIONS:
        raise ExtractionError(
            f"Unsupported file type '{ext}' for {raw.filename}. "
            f"Accepted: {', '.join(SUPPORTED_EXTENSIONS)}"
        )

    if ext == ".pdf":
        return _extract_pdf(raw.data)
    if ext in (".tgz", ".tar.gz"):
        return _extract_tgz(raw.data, remaining_budget)
    if ext == ".json":
        try:
            parsed = json.loads(_decode_text(raw.data))
            return json.dumps(parsed, indent=2, sort_keys=True)
        except json.JSONDecodeError:
            return _decode_text(raw.data)
    return _decode_text(raw.data)


def extract_files(uploads: list[tuple[str, bytes]]) -> list[ExtractedFile]:
    """Extract text from each already-fully-read upload.

    Re-checks the file-count and per-file size caps here as defense in
    depth. The caller is expected to have already enforced both, incrementally,
    while reading the request body (routes/generate.py's chunked
    `_read_uploads`, issue #705), so an oversized or excessive upload is
    rejected before it is ever fully buffered in memory; this function does
    not defend against that on its own, since by the time it runs, every
    upload in `uploads` is already fully read into memory.
    """
    if len(uploads) > settings.upload_max_files:
        raise ExtractionError(
            f"Too many files: limit is {settings.upload_max_files}, got {len(uploads)}"
        )

    results: list[ExtractedFile] = []
    remaining = settings.upload_max_extracted_chars

    for filename, data in uploads:
        if len(data) > settings.upload_max_file_bytes:
            raise ExtractionError(
                f"File '{filename}' is {len(data)} bytes, exceeds limit "
                f"of {settings.upload_max_file_bytes}"
            )
        if remaining <= 0:
            results.append(ExtractedFile(filename=filename, text="", truncated=True))
            continue

        text = _extract_one(_Raw(filename=filename, data=data), remaining_budget=remaining)
        truncated = False
        if len(text) > remaining:
            text = text[:remaining]
            truncated = True
        remaining -= len(text)
        results.append(ExtractedFile(filename=filename, text=text, truncated=truncated))

    return results


def render_file_context(files: list[ExtractedFile]) -> str:
    """Build a labeled block of extracted text to feed alongside the prompt."""
    if not files:
        return ""
    sections: list[str] = []
    for f in files:
        suffix = " (truncated)" if f.truncated else ""
        sections.append(f"=== FILE: {f.filename}{suffix} ===\n{f.text}")
    return "\n\n".join(sections)
