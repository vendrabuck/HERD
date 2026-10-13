"""The driver upload's bounded read of driver_metadata.json (issue #1180, item 3).

The archive is capped compressed (DRIVER_MAX_SIZE_BYTES), but before #1180 the upload
read the metadata entry whole, so a small, highly compressible entry expanded in
memory in this process, and its tar reader read a pax or GNU long-name header whole.
Now a metadata entry over MAX_DRIVER_METADATA_BYTES, declared or read, and a tar
header record over herd_common's MAX_TAR_READ_BYTES are refused with a 422 naming the
bound, and nothing is stored. A malformed metadata file is still the closed default.
"""

import gzip
import io
import json
import struct
import tarfile
import tracemalloc
import zipfile
import zlib
from unittest.mock import patch

import pytest
from app.database import Base, get_db
from app.dependencies.auth import get_current_user_payload
from app.main import app
from app.models.driver_package import DriverPackage
from app.services import driver_service
from app.services.driver_service import (
    MAX_DRIVER_METADATA_BYTES,
    _parse_driver_metadata,
)
from fastapi import HTTPException
from herd_common.bounded_tar import MAX_TAR_READ_BYTES
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

METADATA_REFUSAL = "driver_metadata.json is larger than 1048576 bytes; nothing was stored"
TAR_READ_REFUSAL = (
    "Driver package has a tar header record over 1048576 bytes "
    "(a pax extended header or GNU long name); nothing was stored"
)

_storage: dict[str, bytes] = {}


async def _override_get_db():
    async with TestSessionLocal() as session:
        yield session


def _override_admin():
    return {"sub": "00000000-0000-0000-0000-000000000001", "username": "admin", "role": "admin"}


@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    _storage.clear()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def client():
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user_payload] = _override_admin
    with (
        patch.object(driver_service, "upload_object", side_effect=_storage.__setitem__),
        patch.object(driver_service, "delete_object", side_effect=_storage.pop),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            yield ac
    app.dependency_overrides.clear()


async def _stored_rows() -> list[DriverPackage]:
    async with TestSessionLocal() as db:
        return list((await db.execute(select(DriverPackage))).scalars().all())


async def _row_count() -> int:
    async with TestSessionLocal() as db:
        return (await db.execute(select(func.count()).select_from(DriverPackage))).scalar()


def _metadata_of_size(size: int, **flags) -> bytes:
    """A valid JSON object of exactly `size` bytes carrying `flags`."""
    head = json.dumps({**flags, "pad": ""}).encode()
    assert size >= len(head)
    return head[:-2] + b"x" * (size - len(head)) + b'"}'


def _zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _tar_gz(entries: dict[str, bytes], *, tar_format=tarfile.GNU_FORMAT, pax_comment=None):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tar_format) as tf:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            if pax_comment is not None:
                info.pax_headers = {"comment": pax_comment}
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _package(kind: str, metadata: bytes) -> tuple[str, bytes]:
    entries = {"driver.py": b"class Driver:\n    pass\n", "driver_metadata.json": metadata}
    if kind == "zip":
        return "driver.zip", _zip(entries)
    return "driver.tar.gz", _tar_gz(entries)


async def _upload(client, filename: str, data: bytes, name: str = "D"):
    return await client.post(
        "/drivers",
        data={"name": name, "connection_type": "Management"},
        files={"file": (filename, io.BytesIO(data), "application/octet-stream")},
    )


def test_metadata_bound_is_the_documented_constant_and_fits_the_tar_read_bound():
    assert MAX_DRIVER_METADATA_BYTES == 1024 * 1024
    # A tar metadata member is read in one call of at most its declared size through
    # BoundedTarStream, so a larger metadata bound would refuse an honest file there.
    assert MAX_DRIVER_METADATA_BYTES <= MAX_TAR_READ_BYTES


# --- An entry that declares and holds more than the bound is refused at upload ---


@pytest.mark.parametrize("kind", ["zip", "tar"])
async def test_oversized_metadata_is_refused_and_nothing_is_stored(client, kind):
    filename, data = _package(kind, _metadata_of_size(MAX_DRIVER_METADATA_BYTES + 1))
    # The compressed upload is far under the archive cap; only decompression is large.
    assert len(data) < 64 * 1024

    resp = await _upload(client, filename, data)

    assert resp.status_code == 422
    assert resp.json()["detail"] == METADATA_REFUSAL
    assert _storage == {}
    assert await _row_count() == 0


@pytest.mark.parametrize("kind", ["zip", "tar"])
async def test_metadata_exactly_at_the_bound_still_uploads(client, kind):
    metadata = _metadata_of_size(MAX_DRIVER_METADATA_BYTES, supports_dry_run=True)
    filename, data = _package(kind, metadata)

    resp = await _upload(client, filename, data)

    assert resp.status_code == 201, resp.text
    assert resp.json()["supports_dry_run"] is True
    assert list(_storage.values()) == [data]


@pytest.mark.parametrize("kind", ["zip", "tar"])
async def test_oversized_metadata_on_file_replace_leaves_the_stored_file(client, kind):
    good_name, good = _package(kind, json.dumps({"supports_dry_run": True}).encode())
    created = await _upload(client, good_name, good)
    assert created.status_code == 201
    driver_id = created.json()["id"]
    before = await _stored_rows()

    bad_name, bad = _package(kind, _metadata_of_size(MAX_DRIVER_METADATA_BYTES + 1))
    resp = await client.put(
        f"/drivers/{driver_id}/file",
        files={"file": (bad_name, io.BytesIO(bad), "application/octet-stream")},
    )

    assert resp.status_code == 422
    assert resp.json()["detail"] == METADATA_REFUSAL
    assert list(_storage.values()) == [good]
    after = await _stored_rows()
    assert [(r.sha256, r.storage_key, r.supports_dry_run) for r in after] == [
        (r.sha256, r.storage_key, r.supports_dry_run) for r in before
    ]


@pytest.mark.parametrize("kind", ["zip", "tar"])
def test_a_large_declared_size_is_refused_before_it_is_decompressed(kind):
    _, data = _package(kind, b" " * (16 * MAX_DRIVER_METADATA_BYTES))
    filename = "d.zip" if kind == "zip" else "d.tar.gz"

    tracemalloc.start()
    try:
        with pytest.raises(HTTPException) as exc:
            _parse_driver_metadata(filename, data)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert exc.value.status_code == 422
    assert exc.value.detail == METADATA_REFUSAL
    # Refused on the declared size: not even the capped read ran.
    assert peak < MAX_DRIVER_METADATA_BYTES // 2


# --- An entry whose header understates what it holds ---


class _UnboundedEntry(io.BytesIO):
    """A zip entry reader that ignores the declared size, as a reader other than
    the standard library's might; the cap must not rely on the reader honoring it."""


def test_an_entry_holding_more_than_it_declares_is_refused_by_the_read_cap():
    _, data = _package("zip", b"{}")
    longer = _UnboundedEntry(b" " * (MAX_DRIVER_METADATA_BYTES + 10))

    with patch.object(zipfile.ZipFile, "open", return_value=longer):
        with pytest.raises(HTTPException) as exc:
            _parse_driver_metadata("d.zip", data)

    assert exc.value.status_code == 422
    assert exc.value.detail == METADATA_REFUSAL


def _zip_understating(actual: bytes, declared: int) -> bytes:
    """A zip whose local and central headers declare `declared` bytes (with the CRC of
    that prefix) while the deflate stream holds all of `actual`."""
    data = bytearray(_zip({"driver.py": b"x = 1\n", "driver_metadata.json": actual}))
    crc = zlib.crc32(actual[:declared])
    name = b"driver_metadata.json"
    for sig, crc_off, size_off, name_off in (
        (b"PK\x03\x04", 14, 22, 30),
        (b"PK\x01\x02", 16, 24, 46),
    ):
        at = 0
        while True:
            at = data.index(sig, at)
            if data[at + name_off : at + name_off + len(name)] == name:
                break
            at += 1
        struct.pack_into("<I", data, at + crc_off, crc)
        struct.pack_into("<I", data, at + size_off, declared)
    return bytes(data)


def test_a_real_zip_understating_its_entry_decompresses_only_up_to_the_read_cap():
    """zipfile returns at most the central directory's file_size, but it decompresses
    up to the size it was asked for before truncating: asked for everything (the
    pre-#1180 ``zf.read``), it inflates the whole 16 MiB stream. The capped read
    keeps that to about MAX_DRIVER_METADATA_BYTES, and the declared prefix parses."""
    head = json.dumps({"supports_dry_run": True}).encode()
    data = _zip_understating(head + b" " * (16 * MAX_DRIVER_METADATA_BYTES), len(head))

    tracemalloc.start()
    try:
        meta = _parse_driver_metadata("d.zip", data)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert meta == {"supports_dry_run": True}
    assert peak < 4 * MAX_DRIVER_METADATA_BYTES


# --- A tar header record over the read bound is refused before it is read ---


def _tar_gz_with_oversized_header(header_type: bytes, declared: int) -> bytes:
    header = tarfile.TarInfo("././@header")
    header.type = header_type
    header.size = declared
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        gz.write(header.tobuf(format=tarfile.GNU_FORMAT))
        gz.write(b"\0" * declared)
        gz.write(b"\0" * ((512 - declared % 512) % 512))
        meta = tarfile.TarInfo("driver_metadata.json")
        payload = b'{"supports_dry_run": true}'
        meta.size = len(payload)
        gz.write(meta.tobuf(format=tarfile.GNU_FORMAT))
        gz.write(payload + b"\0" * ((512 - len(payload) % 512) % 512))
        gz.write(b"\0" * 1024)
    return buf.getvalue()


_HEADER_TYPES = {
    "pax-x": tarfile.XHDTYPE,
    "pax-g": tarfile.XGLTYPE,
    "gnu-L": tarfile.GNUTYPE_LONGNAME,
    "gnu-K": tarfile.GNUTYPE_LONGLINK,
}


@pytest.mark.parametrize("header_type", _HEADER_TYPES.values(), ids=_HEADER_TYPES)
async def test_a_tar_header_record_over_the_read_bound_is_refused_at_upload(client, header_type):
    data = _tar_gz_with_oversized_header(header_type, 8 * MAX_TAR_READ_BYTES)

    resp = await _upload(client, "driver.tar.gz", data)

    assert resp.status_code == 422
    assert resp.json()["detail"] == TAR_READ_REFUSAL
    assert _storage == {}
    assert await _row_count() == 0


def test_a_tar_header_record_over_the_read_bound_is_never_read_into_memory():
    data = _tar_gz_with_oversized_header(tarfile.XHDTYPE, 8 * MAX_TAR_READ_BYTES)

    tracemalloc.start()
    try:
        with pytest.raises(HTTPException) as exc:
            _parse_driver_metadata("d.tar.gz", data)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert exc.value.detail == TAR_READ_REFUSAL
    assert peak < 2 * MAX_TAR_READ_BYTES


async def test_a_pax_header_under_the_read_bound_still_uploads(client):
    data = _tar_gz(
        {"driver_metadata.json": b'{"supports_vrf": true}'},
        tar_format=tarfile.PAX_FORMAT,
        pax_comment="c" * (MAX_TAR_READ_BYTES - 4096),
    )

    resp = await _upload(client, "driver.tar.gz", data)

    assert resp.status_code == 201, resp.text
    assert resp.json()["supports_vrf"] is True


# --- The closed default for a malformed file is unchanged ---


@pytest.mark.parametrize("kind", ["zip", "tar"])
async def test_malformed_metadata_still_uploads_with_flags_closed(client, kind):
    filename, data = _package(kind, b"\xff not json")

    resp = await _upload(client, filename, data)

    assert resp.status_code == 201
    assert resp.json()["supports_dry_run"] is False
    assert resp.json()["supports_vrf"] is False
