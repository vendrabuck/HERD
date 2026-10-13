"""A decompressed tar stream that refuses any single read over a fixed bound.

tarfile reads member headers in 512-byte blocks and copies member data in 16 KiB
chunks, but it reads a pax extended header (types x and g) or a GNU long name or
long link (types L and K) whole, in one call, before the member it describes exists
to be checked against any ceiling. A small, highly compressible archive can declare
such a record of hundreds of megabytes, and the reader would allocate all of it.
Bounding every single read bounds that allocation (issues #1179 and #1180). A real
pax header or long name is well under a kilobyte.

Every reader of an uploaded tar opens it as::

    with gzip.GzipFile(fileobj=io.BytesIO(data), mode="rb") as raw:
        tf = tarfile.open(fileobj=BoundedTarStream(raw), mode="r:")

and maps ``TarReadLimitError`` to its own refusal: execution's driver loader to its
``PackageLimitError``, inventory's driver upload to a 422. Opening with
``tarfile.open(mode="r:gz")`` instead reads through tarfile's own gzip stream, which
this bound cannot see, so do not swap it back.

The bound relies on tarfile's read sizes: a header block is 512 bytes and member data
is copied in chunks of at most 16 KiB, so a well-formed package never reaches it. If a
future tarfile read member data in one call, a member larger than the bound would be
refused; execution's ``test_member_data_larger_than_the_read_bound_extracts`` and the
long-name tests fail if that ever changes. A caller that reads one member whole
(``extractfile(member).read()``) makes a single read of the member's size, so it must
bound the member at or under MAX_TAR_READ_BYTES first, as inventory's metadata read
does.
"""

import io
from typing import Protocol

MAX_TAR_READ_BYTES = 1024 * 1024

# The HERD-authored description of a refused read; each caller wraps it in its own
# sentence (execution: "Driver package has a <this>; nothing was extracted").
TAR_READ_LIMIT_TEXT = (
    f"tar header record over {MAX_TAR_READ_BYTES} bytes (a pax extended header or GNU long name)"
)


class TarReadLimitError(ValueError):
    """A single read from the decompressed tar stream asked for more than MAX_TAR_READ_BYTES.

    The message is ``TAR_READ_LIMIT_TEXT``, HERD-authored, with no path in it.
    """


class _Readable(Protocol):
    def read(self, size: int = -1) -> bytes: ...

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int: ...

    def tell(self) -> int: ...


class BoundedTarStream:
    """The decompressed tar stream, refusing any single read over MAX_TAR_READ_BYTES.

    tarfile reads a pax or GNU long-name header's whole declared size in one call;
    this refuses that call before anything is decompressed into memory for it.
    Every other read tarfile makes is a 512-byte header block or a data chunk of at
    most its 16 KiB copy buffer, so a well-formed package never reaches the bound.
    An unbounded read (``read()`` or a negative size) is refused as well.
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
