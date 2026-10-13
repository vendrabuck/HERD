"""herd_common.bounded_tar: the one bounded tar stream (issues #1179 and #1180).

The end-to-end refusals live with the callers: execution's
tests/test_driver_loader_security.py and inventory's
tests/test_driver_metadata_bounds.py. These pin the stream itself.
"""

import gzip
import io
import shutil
import tarfile

import pytest
from herd_common.bounded_tar import (
    MAX_TAR_READ_BYTES,
    TAR_READ_LIMIT_TEXT,
    BoundedTarStream,
    TarReadLimitError,
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
    return gz, tarfile.open(fileobj=BoundedTarStream(gz), mode="r:")


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
