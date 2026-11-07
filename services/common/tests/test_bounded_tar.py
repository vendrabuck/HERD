"""herd_common.bounded_tar: the one bounded tar reader (issues #1179, #1180, #1279).

The end-to-end refusals live with the callers: execution's
tests/test_driver_loader_security.py, inventory's tests/test_driver_metadata_bounds.py,
and ai-orchestrator's tests/test_extractor_branches.py. These pin the stream, the header
check, and the open helper themselves.
"""

import gzip
import io
import shutil
import tarfile

import pytest
from herd_common.bounded_tar import (
    EXTENDED_HEADER_TYPES,
    MAX_TAR_READ_BYTES,
    TAR_READ_LIMIT_TEXT,
    BoundedTarInfo,
    BoundedTarStream,
    TarReadLimitError,
    open_bounded_tar,
)


def test_the_bound_and_its_text_are_the_documented_values():
    assert MAX_TAR_READ_BYTES == 1024 * 1024
    assert TAR_READ_LIMIT_TEXT == (
        "tar header record over 1048576 bytes (a pax extended header or GNU long name)"
    )
    assert issubclass(TarReadLimitError, ValueError)


@pytest.mark.parametrize("size", [MAX_TAR_READ_BYTES + 1, -1, None])
def test_a_read_over_the_bound_or_unbounded_is_refused_without_reading(size):
    raw = io.BytesIO(b"\0" * (2 * MAX_TAR_READ_BYTES))
    with pytest.raises(TarReadLimitError) as exc:
        BoundedTarStream(raw).read(size)
    assert str(exc.value) == TAR_READ_LIMIT_TEXT
    assert raw.tell() == 0


def test_a_read_at_the_bound_is_passed_through():
    raw = io.BytesIO(b"a" * (MAX_TAR_READ_BYTES + 5))
    stream = BoundedTarStream(raw)
    assert stream.read(MAX_TAR_READ_BYTES) == b"a" * MAX_TAR_READ_BYTES
    assert stream.tell() == MAX_TAR_READ_BYTES
    assert stream.seek(1) == 1
    assert stream.read(2) == b"aa"


def _open_through_stream(data: bytes, name: str):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT) as tf:
        info = tarfile.TarInfo(name)
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    gz = gzip.GzipFile(fileobj=io.BytesIO(buf.getvalue()), mode="rb")
    return gz, open_bounded_tar(gz)


def test_member_data_over_the_bound_is_read_in_chunks():
    data = bytes(range(256)) * (3 * MAX_TAR_READ_BYTES // 256)
    gz, tf = _open_through_stream(data, "n" * 300)
    with gz, tf:
        (member,) = tf.getmembers()
        assert member.name == "n" * 300
        extracted = tf.extractfile(member)
        assert extracted is not None
        out = io.BytesIO()
        shutil.copyfileobj(extracted, out, 16 * 1024)
        assert out.getvalue() == data


def test_one_whole_read_of_member_data_over_the_bound_is_refused():
    """Why a caller that reads a member whole must bound the member first (inventory
    refuses a driver_metadata.json over MAX_DRIVER_METADATA_BYTES before reading)."""
    gz, tf = _open_through_stream(b"x" * (MAX_TAR_READ_BYTES + 1), "big")
    with gz, tf:
        extracted = tf.extractfile("big")
        assert extracted is not None
        with pytest.raises(TarReadLimitError):
            extracted.read()


# --- The header check (issue #1279) ---
#
# Python 3.13.18 reads a pax or GNU long-name record in 1 MiB chunks (tarfile's
# _safe_read), so a per-read bound of 1 MiB no longer refuses one. BoundedTarInfo
# refuses the header at its 512-byte block from the size it declares, before any of
# the record is read, whatever size of reads tarfile would make.

_TYPE_IDS = {
    tarfile.XHDTYPE: "pax-x",
    tarfile.XGLTYPE: "pax-g",
    tarfile.SOLARIS_XHDTYPE: "solaris-X",
    tarfile.GNUTYPE_LONGNAME: "gnu-L",
    tarfile.GNUTYPE_LONGLINK: "gnu-K",
}
_EXTENDED_TYPES = sorted(EXTENDED_HEADER_TYPES)


class _CountingRaw(io.BytesIO):
    """An uncompressed tar stream that records how many bytes were read from it."""

    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.bytes_read = 0

    def read(self, size: int | None = -1) -> bytes:
        out = super().read(size)
        self.bytes_read += len(out)
        return out


def _tar_with_header(header_type: bytes, declared: int, fill: bytes = b"\0") -> bytes:
    """An uncompressed tar whose first header is `header_type` declaring `declared`
    bytes, followed by that many real bytes and one small member, so a reader that
    honors the header does read the whole record."""
    header = tarfile.TarInfo("././@header")
    header.type = header_type
    header.size = declared
    member = tarfile.TarInfo("a.txt")
    member.size = 2
    return (
        header.tobuf(format=tarfile.GNU_FORMAT)
        + fill * declared
        + b"\0" * ((512 - declared % 512) % 512)
        + member.tobuf(format=tarfile.GNU_FORMAT)
        + b"hi"
        + b"\0" * 510
        + b"\0" * 1024
    )


def test_the_extended_header_types_are_the_five_tarfile_reads_a_record_for():
    assert EXTENDED_HEADER_TYPES == {b"x", b"g", b"X", b"L", b"K"}


def test_tarfile_still_has_the_methods_bounded_tar_info_uses():
    """BoundedTarInfo overrides the private _proc_member and calls the private _block;
    a Python upgrade that renames either fails here rather than silently dropping the
    header check."""
    assert callable(tarfile.TarInfo._proc_member)
    assert tarfile.TarInfo("x")._block(1) == 512
    assert tarfile.TarInfo("x")._block(512) == 512
    assert BoundedTarInfo._proc_member is not tarfile.TarInfo._proc_member


@pytest.mark.parametrize("header_type", _EXTENDED_TYPES, ids=lambda t: _TYPE_IDS[t])
def test_an_extended_header_over_the_bound_is_refused_at_its_header_block(header_type):
    raw = _CountingRaw(_tar_with_header(header_type, 8 * MAX_TAR_READ_BYTES))
    with pytest.raises(TarReadLimitError) as exc:
        open_bounded_tar(raw)
    assert str(exc.value) == TAR_READ_LIMIT_TEXT
    # Only the 512-byte header block was read; none of the 8 MiB record was.
    assert raw.bytes_read == 512


@pytest.mark.parametrize("header_type", _EXTENDED_TYPES, ids=lambda t: _TYPE_IDS[t])
def test_the_header_check_alone_refuses_without_the_read_bound(header_type):
    """BoundedTarInfo on a stream with no per-read bound: the refusal does not depend on
    how tarfile reads the record (one read on 3.12, 1 MiB chunks on 3.13.18)."""
    raw = _CountingRaw(_tar_with_header(header_type, 8 * MAX_TAR_READ_BYTES))
    with pytest.raises(TarReadLimitError):
        tarfile.open(fileobj=raw, mode="r:", tarinfo=BoundedTarInfo)
    assert raw.bytes_read == 512


def test_control_a_plain_tarinfo_reads_the_whole_record():
    """The control for the two tests above: the same archive opened without the header
    check reads all 8 MiB of the record, so the 512 bytes above are the check's doing."""
    declared = 8 * MAX_TAR_READ_BYTES
    raw = _CountingRaw(_tar_with_header(tarfile.GNUTYPE_LONGNAME, declared, fill=b"n"))
    with tarfile.open(fileobj=raw, mode="r:") as tf:
        (member,) = tf.getmembers()
    assert raw.bytes_read >= declared
    assert len(member.name) == declared


def test_a_long_name_record_at_the_bound_is_read_and_one_byte_over_is_refused():
    at = _tar_with_header(tarfile.GNUTYPE_LONGNAME, MAX_TAR_READ_BYTES, fill=b"n")
    with open_bounded_tar(io.BytesIO(at)) as tf:
        (member,) = tf.getmembers()
    assert member.name == "n" * MAX_TAR_READ_BYTES
    over = _tar_with_header(tarfile.GNUTYPE_LONGNAME, MAX_TAR_READ_BYTES + 1, fill=b"n")
    with pytest.raises(TarReadLimitError):
        open_bounded_tar(io.BytesIO(over))


@pytest.mark.parametrize("tar_format", [tarfile.PAX_FORMAT, tarfile.GNU_FORMAT], ids=["pax", "gnu"])
def test_a_3000_byte_path_extracts_through_both_guards(tar_format):
    name = "/".join(["d" * 99] * 30) + "/f.txt"
    assert len(name) > 3000
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tar_format) as tf:
        info = tarfile.TarInfo(name)
        info.size = 3
        info.pax_headers = {"comment": "c" * 2000} if tar_format == tarfile.PAX_FORMAT else {}
        tf.addfile(info, io.BytesIO(b"abc"))
    with open_bounded_tar(io.BytesIO(buf.getvalue())) as tf:
        (member,) = tf.getmembers()
        assert member.name == name
        extracted = tf.extractfile(member)
        assert extracted is not None
        assert extracted.read(3) == b"abc"


def test_a_subclass_runs_its_own_checks_then_the_header_check():
    seen = []

    class _Recording(BoundedTarInfo):
        def _proc_member(self, archive):
            seen.append(self.type)
            return super()._proc_member(archive)

    raw = io.BytesIO(_tar_with_header(tarfile.XHDTYPE, 8 * MAX_TAR_READ_BYTES))
    with pytest.raises(TarReadLimitError):
        open_bounded_tar(raw, tarinfo=_Recording)
    assert seen == [tarfile.XHDTYPE]


@pytest.mark.parametrize("tarinfo", [tarfile.TarInfo, object, None, BoundedTarInfo("x")])
def test_open_bounded_tar_refuses_a_tarinfo_without_the_header_check(tarinfo):
    raw = io.BytesIO(_tar_with_header(tarfile.XHDTYPE, 8 * MAX_TAR_READ_BYTES))
    with pytest.raises(TypeError, match="tarinfo must be BoundedTarInfo or a subclass of it"):
        open_bounded_tar(raw, tarinfo=tarinfo)
    assert raw.tell() == 0
