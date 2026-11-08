"""The one bounded reader of an uploaded tar: a header check and a read bound.

A pax extended header (types x and g, and Solaris X) or a GNU long name or long link
(types L and K) carries its record in the data that follows its 512-byte header block,
and tarfile reads that data before the member it describes exists to be checked
against any ceiling. A small, highly compressible archive can declare such a record of
hundreds of megabytes, and the reader would allocate all of it. A real pax header or
long name is well under a kilobyte. Two guards bound it (issues #1179, #1180, #1192,
and #1279):

- ``BoundedTarInfo`` refuses, with ``TarReadLimitError``, any such header whose
  declared size rounded up to the 512-byte block (``_block(size)``, the bytes tarfile
  reads for it) is over MAX_TAR_READ_BYTES. It runs in ``_proc_member``, once the
  header block is read and before any of the record is, so it does not depend on how
  tarfile reads the record. This is the bound since #1279: Python 3.13.18 reads an
  extended header's record in 1 MiB chunks (tarfile's ``_safe_read``, gh-151497)
  instead of in one read, and every chunk passes a per-read bound of 1 MiB.
- ``BoundedTarStream`` refuses any single read over MAX_TAR_READ_BYTES, and an
  unbounded read. On a tarfile that reads the record in one call (3.12, and 3.13
  before 3.13.18) it refused the same headers on its own; it stays as the second
  line, and it is what bounds a caller's own member reads (below).

Every reader of an uploaded tar opens it through ``open_bounded_tar``::

    with gzip.GzipFile(fileobj=io.BytesIO(data), mode="rb") as raw:
        tf = open_bounded_tar(raw)

which is ``tarfile.open(fileobj=BoundedTarStream(raw), mode="r:", tarinfo=...)`` with
``BoundedTarInfo`` or a subclass of it, and maps ``TarReadLimitError`` to its own
refusal: execution's driver loader to its ``PackageLimitError``, inventory's driver
upload to a 422, ai-orchestrator's context file extractor to its ``ExtractionError``
(a 400). ai-orchestrator passes its own subclass, whose ceilings run first. Opening
with ``tarfile.open(mode="r:gz")`` instead reads through tarfile's own gzip stream,
which the read bound cannot see, so do not swap it back.

The stream bounds one read, not how far the stream goes: many reads under the bound
can still pass over any amount of data. A reader that must bound that as well does it
itself. ai-orchestrator's extractor puts its own position ceiling (``_CeilingStream``)
around the gzip stream and gives that to ``open_bounded_tar`` as the raw stream
(issue #1195).

That mode also turned a body that is not gzip into ``tarfile.ReadError`` at the open. A
hand-opened ``GzipFile`` reads lazily and raises its own errors at whichever read first
meets the fault: ``BadGzipFile`` (an ``OSError``) for a body that is not gzip or fails
its checksum, ``EOFError`` for a stream that ends early, ``zlib.error`` for corrupt
data. Each reader decides what those become: execution re-raises an ``OSError`` from
the open as ``tarfile.ReadError`` and its load path treats any extraction failure as a
broken package, inventory's metadata read takes any failure as no metadata, and
ai-orchestrator's extractor raises all three as ``tarfile.ReadError``.

The read bound relies on tarfile's other read sizes: a header block is 512 bytes and
member data is copied in chunks of at most 16 KiB, so a well-formed package never
reaches it. If a future tarfile read member data in one call, a member larger than the
bound would be refused; execution's ``test_member_data_larger_than_the_read_bound_extracts``
and the long-name tests fail if that ever changes. A caller that reads a member itself
passes its read size through to this stream: ``extractfile(member).read(n)`` reaches it
as one read of up to ``n`` bytes, and ``read()`` as one read of the member's whole size.
Such a caller must either bound the member at or under MAX_TAR_READ_BYTES first, as
inventory's metadata read does, or read the member in chunks of at most
MAX_TAR_READ_BYTES, as ai-orchestrator's extractor does for a text member that may be
several megabytes.

Limits. ``BoundedTarInfo`` overrides tarfile's private ``_proc_member`` and uses its
private ``_block``; both are the same in 3.12 to 3.14, and
``test_tarfile_still_has_the_methods_bounded_tar_info_uses`` fails if an upgrade
renames them. The header check bounds one extended header, not a run of them in front
of one member (each passes on its own; ai-orchestrator's subclass bounds the run, the
driver readers do not) and not what tarfile builds from a record it read. A tarfile
that read a record by some path other than ``_proc_member`` would bypass the header
check and meet only the stream's per-read bound.
"""

import io
import tarfile
from typing import Protocol

MAX_TAR_READ_BYTES = 1024 * 1024

# The HERD-authored description of a refused read; each caller wraps it in its own
# sentence (execution: "Driver package has a <this>; nothing was extracted").
TAR_READ_LIMIT_TEXT = (
    f"tar header record over {MAX_TAR_READ_BYTES} bytes (a pax extended header or GNU long name)"
)


class TarReadLimitError(ValueError):
    """A tar header record, or a single read from the stream, over MAX_TAR_READ_BYTES.

    The message is ``TAR_READ_LIMIT_TEXT``, HERD-authored, with no path in it.
    """


class _Readable(Protocol):
    def read(self, size: int = -1) -> bytes: ...

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int: ...

    def tell(self) -> int: ...


class BoundedTarStream:
    """The decompressed tar stream, refusing any single read over MAX_TAR_READ_BYTES.

    The second line behind ``BoundedTarInfo``: a tarfile that reads a pax or GNU
    long-name header's whole declared size in one call is refused at that call before
    anything is decompressed into memory for it. Every other read tarfile makes is a
    512-byte header block or a data chunk of at most its 16 KiB copy buffer, so a
    well-formed package never reaches the bound. An unbounded read (``read()`` or a
    negative size) is refused as well.
    """

    def __init__(self, raw: _Readable) -> None:
        self._raw = raw

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0 or size > MAX_TAR_READ_BYTES:
            raise TarReadLimitError(TAR_READ_LIMIT_TEXT)
        return self._raw.read(size)

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        return self._raw.seek(offset, whence)

    def tell(self) -> int:
        return self._raw.tell()


# The header types whose record tarfile reads from the data after the header block:
# pax extended (x), pax global (g), Solaris extended (X), GNU long name (L), and GNU
# long link (K).
EXTENDED_HEADER_TYPES = frozenset(
    {
        tarfile.XHDTYPE,
        tarfile.XGLTYPE,
        tarfile.SOLARIS_XHDTYPE,
        tarfile.GNUTYPE_LONGNAME,
        tarfile.GNUTYPE_LONGLINK,
    }
)


class BoundedTarInfo(tarfile.TarInfo):
    """The TarInfo class every uploaded tar is opened with (issue #1279).

    tarfile builds every header through this class (``tarfile.open(tarinfo=...)``) and
    calls ``_proc_member`` once a header's 512-byte block is read and before any data
    of that header is. An extended header (``EXTENDED_HEADER_TYPES``) whose record,
    rounded up to the block tarfile reads, is over MAX_TAR_READ_BYTES is refused there,
    whatever size of reads tarfile would make for it. A subclass that adds its own
    checks runs them and then calls ``super()._proc_member``.
    """

    def _proc_member(self, archive):
        if self.type in EXTENDED_HEADER_TYPES and self._block(self.size) > MAX_TAR_READ_BYTES:
            raise TarReadLimitError(TAR_READ_LIMIT_TEXT)
        return super()._proc_member(archive)


def open_bounded_tar(
    raw: _Readable, *, tarinfo: type[BoundedTarInfo] = BoundedTarInfo
) -> tarfile.TarFile:
    """Open the decompressed tar stream `raw` for reading through both guards.

    `tarinfo` must be ``BoundedTarInfo`` or a subclass of it; anything else is a
    ``TypeError``, so no caller can open an uploaded tar without the header check.
    """
    if not (isinstance(tarinfo, type) and issubclass(tarinfo, BoundedTarInfo)):
        raise TypeError("tarinfo must be BoundedTarInfo or a subclass of it")
    return tarfile.open(fileobj=BoundedTarStream(raw), mode="r:", tarinfo=tarinfo)
