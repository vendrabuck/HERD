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


class ExtractionError(Exception):
    """Raised for invalid, oversized, or unparsable files."""


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
    """
    parts: list[str] = []
    used = 0
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data), mode="rb") as gz:
            try:
                with tarfile.open(fileobj=BoundedTarStream(gz), mode="r:") as tar:
                    for member in tar:
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
                        header = f"--- {member.name} ---\n"
                        chunk = header + text
                        parts.append(chunk)
                        used += len(chunk)
            except _GZIP_FAILURES as e:
                raise tarfile.ReadError("not a readable gzip stream") from e
    except TarReadLimitError as e:
        raise ExtractionError(TGZ_READ_LIMIT_DETAIL) from e
    except tarfile.TarError as e:
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
