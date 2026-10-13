import gzip
import hashlib
import io
import json
import logging
import tarfile
import uuid
import zipfile
from typing import IO

from fastapi import HTTPException
from herd_common.bounded_tar import TAR_READ_LIMIT_TEXT, BoundedTarStream, TarReadLimitError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.driver_package import VALID_CONNECTION_TYPES, ConnectionType, DriverPackage
from app.models.template import DeviceTemplate
from app.storage import delete_object, download_object, upload_object

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {".zip", ".tar.gz"}


# The largest driver_metadata.json the upload will read (issue #1180). The archive is
# capped compressed (DRIVER_MAX_SIZE_BYTES), but a small, highly compressible entry
# could otherwise expand in memory in this process. A real metadata file is a few
# hundred bytes. It must not exceed MAX_TAR_READ_BYTES: a tar member is read in one
# call of at most its declared size through BoundedTarStream.
MAX_DRIVER_METADATA_BYTES = 1024 * 1024

METADATA_TOO_LARGE_DETAIL = (
    f"driver_metadata.json is larger than {MAX_DRIVER_METADATA_BYTES} bytes; nothing was stored"
)
TAR_READ_LIMIT_DETAIL = f"Driver package has a {TAR_READ_LIMIT_TEXT}; nothing was stored"


class _MetadataTooLargeError(ValueError):
    """driver_metadata.json declares, or holds, more than MAX_DRIVER_METADATA_BYTES."""


def _read_metadata_bytes(stream: IO[bytes], declared_size: int) -> bytes:
    """Read one metadata entry, never more than MAX_DRIVER_METADATA_BYTES plus one byte.

    The declared size (zip ``file_size``, tar ``size``) is checked first, so an entry
    declaring too much is refused before anything is decompressed for it; then the
    read itself is capped, so an entry whose header understates what it holds is
    refused rather than read whole. The cap also bounds the work behind the read:
    zipfile returns at most the central directory's ``file_size``, but it inflates up
    to the size it is asked for before truncating, so an unbounded read of an entry
    that understates its size would inflate the whole stream. tarfile reads exactly
    a member's ``size``.
    """
    if declared_size > MAX_DRIVER_METADATA_BYTES:
        raise _MetadataTooLargeError
    raw = stream.read(MAX_DRIVER_METADATA_BYTES + 1)
    if len(raw) > MAX_DRIVER_METADATA_BYTES:
        raise _MetadataTooLargeError
    return raw


def _parse_driver_metadata(filename: str, file_data: bytes) -> dict:
    """Peek inside a driver package for `driver_metadata.json` and return it.

    Returns an empty dict on any failure: unrecognized archive, malformed JSON,
    missing metadata file, a top-level value that is not an object, or anything
    else surprising. Every capability flag read from the result is therefore
    opt-in and closed by default.

    Two failures are refused instead, with a 422 that names the bound and nothing
    stored (issue #1180), because they are about the package's size, not its
    content: a metadata entry over MAX_DRIVER_METADATA_BYTES (declared or read),
    and a tar header record over MAX_TAR_READ_BYTES, which the tar is read through
    ``herd_common.bounded_tar.BoundedTarStream`` to catch before it is read into
    memory.
    """
    lower = filename.lower()
    try:
        if lower.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(file_data)) as zf:
                try:
                    info = zf.getinfo("driver_metadata.json")
                except KeyError:
                    return {}
                with zf.open(info) as entry:
                    raw = _read_metadata_bytes(entry, info.file_size)
        elif lower.endswith(".tar.gz") or lower.endswith(".tgz"):
            with gzip.GzipFile(fileobj=io.BytesIO(file_data), mode="rb") as gz:
                with tarfile.open(fileobj=BoundedTarStream(gz), mode="r:") as tf:
                    try:
                        member = tf.getmember("driver_metadata.json")
                    except KeyError:
                        return {}
                    extracted = tf.extractfile(member)
                    if extracted is None:
                        return {}
                    raw = _read_metadata_bytes(extracted, member.size)
        else:
            return {}
        meta = json.loads(raw.decode("utf-8"))
    except _MetadataTooLargeError:
        raise HTTPException(status_code=422, detail=METADATA_TOO_LARGE_DETAIL)
    except TarReadLimitError:
        raise HTTPException(status_code=422, detail=TAR_READ_LIMIT_DETAIL)
    except Exception as exc:
        logger.info("driver upload: could not parse driver_metadata.json (%s)", exc)
        return {}
    if not isinstance(meta, dict):
        return {}
    return meta


def _parse_supports_dry_run(filename: str, file_data: bytes) -> bool:
    """The package's `supports_dry_run` claim, False unless explicitly declared.

    Only drivers that declare `supports_dry_run: true` are eligible for dry-run
    scheduling.
    """
    return bool(_parse_driver_metadata(filename, file_data).get("supports_dry_run", False))


def _parse_supports_vrf(filename: str, file_data: bytes) -> bool:
    """The package's `supports_vrf` claim, False unless explicitly declared.

    ADR 0014 addendum X-G (issue #755): a Layer 3 driver that declares this
    accepts the `virtual_router` keyword on configure_route/remove_route.
    Execution passes the keyword ONLY to a declaring driver: every shipped L3
    signature ends in `**_`, so a non-declaring driver would swallow it in
    silence, install the route in the default table, and report success.
    """
    return bool(_parse_driver_metadata(filename, file_data).get("supports_vrf", False))


def _validate_filename(filename: str) -> None:
    lower = filename.lower()
    if not any(lower.endswith(ext) for ext in ALLOWED_EXTENSIONS):
        raise HTTPException(
            status_code=422,
            detail=f"Invalid file type: must be one of {sorted(ALLOWED_EXTENSIONS)}",
        )
    # The filename becomes part of the storage key (f"{driver_id}/{filename}"), so
    # reject any path separators or traversal segments up front: a name like
    # "../../etc/x" would otherwise let the upload escape the driver storage root.
    # The storage layer also guards this, but failing here gives a clean 422.
    if "/" in filename or "\\" in filename or ".." in filename or filename.startswith("~"):
        raise HTTPException(
            status_code=422,
            detail="Invalid file name: path separators and traversal segments are not allowed",
        )


async def list_drivers(
    db: AsyncSession, skip: int = 0, limit: int = 50
) -> tuple[list[DriverPackage], int]:
    count_query = select(func.count()).select_from(DriverPackage)
    total = (await db.execute(count_query)).scalar() or 0

    result = await db.execute(
        select(DriverPackage).order_by(DriverPackage.name).offset(skip).limit(limit)
    )
    return list(result.scalars().all()), total


async def get_driver(db: AsyncSession, driver_id: uuid.UUID) -> DriverPackage:
    result = await db.execute(select(DriverPackage).where(DriverPackage.id == driver_id))
    package = result.scalar_one_or_none()
    if not package:
        raise HTTPException(status_code=404, detail="Driver package not found")
    return package


def _validate_connection_type(connection_type: str) -> None:
    if connection_type not in VALID_CONNECTION_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid connection_type: must be one of {sorted(VALID_CONNECTION_TYPES)}",
        )


async def create_driver(
    db: AsyncSession,
    name: str,
    description: str | None,
    connection_type: str,
    filename: str,
    file_data: bytes,
    username: str,
    max_size: int,
) -> DriverPackage:
    _validate_filename(filename)
    _validate_connection_type(connection_type)

    if len(file_data) > max_size:
        raise HTTPException(
            status_code=422,
            detail=f"File too large: max {max_size} bytes",
        )

    sha256 = hashlib.sha256(file_data).hexdigest()
    driver_id = uuid.uuid4()
    storage_key = f"{driver_id}/{filename}"
    metadata = _parse_driver_metadata(filename, file_data)

    upload_object(storage_key, file_data)

    package = DriverPackage(
        id=driver_id,
        name=name,
        description=description,
        connection_type=connection_type,
        filename=filename,
        storage_key=storage_key,
        size_bytes=len(file_data),
        sha256=sha256,
        uploaded_by=username,
        supports_dry_run=bool(metadata.get("supports_dry_run", False)),
        supports_vrf=bool(metadata.get("supports_vrf", False)),
    )
    db.add(package)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        try:
            delete_object(storage_key)
        except Exception:
            pass
        raise HTTPException(
            status_code=409,
            detail=f"Driver with name '{name}' already exists",
        )
    await db.refresh(package)
    return package


async def _assert_templates_allow_connection_type(
    db: AsyncSession, driver_id: uuid.UUID, connection_type: str
) -> None:
    """Refuse a connection-type change that breaks a template using the driver.

    The template-driver contract (template_service._validate_driver_connection_type)
    is checked when a template picks its driver; without this check a driver
    used by a device template could become a Hypervisor recipe, or a dynamic
    template's recipe stop being one, after the fact (issue #1018). The refusal
    is a 409 because the conflict is with other rows, as for a driver delete
    while templates reference it, and it reuses the create-time rule's words.
    """
    template_types = set(
        (
            await db.execute(
                select(DeviceTemplate.template_type)
                .where(DeviceTemplate.driver_id == driver_id)
                .distinct()
            )
        )
        .scalars()
        .all()
    )
    is_hypervisor = connection_type == ConnectionType.HYPERVISOR.value
    if is_hypervisor and "device" in template_types:
        raise HTTPException(
            status_code=409,
            detail=(
                "Cannot change connection_type: device templates use this driver, "
                "and device templates cannot use a Hypervisor-type driver"
            ),
        )
    if not is_hypervisor and "dynamic" in template_types:
        raise HTTPException(
            status_code=409,
            detail=(
                "Cannot change connection_type: dynamic templates use this driver, "
                "and dynamic templates require a Hypervisor-type driver"
            ),
        )


async def update_driver(
    db: AsyncSession,
    driver_id: uuid.UUID,
    name: str | None,
    description: str | None,
    connection_type: str | None = None,
    modified_by: uuid.UUID | None = None,
) -> DriverPackage:
    package = await get_driver(db, driver_id)
    if modified_by is not None:
        package.modified_by = modified_by
    if name is not None:
        package.name = name
    if description is not None:
        package.description = description
    if connection_type is not None:
        _validate_connection_type(connection_type)
        if connection_type != package.connection_type:
            await _assert_templates_allow_connection_type(db, driver_id, connection_type)
        package.connection_type = connection_type
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"Driver with name '{name}' already exists",
        )
    await db.refresh(package)
    return package


async def replace_driver_file(
    db: AsyncSession,
    driver_id: uuid.UUID,
    filename: str,
    file_data: bytes,
    username: str,
    max_size: int,
) -> DriverPackage:
    _validate_filename(filename)

    if len(file_data) > max_size:
        raise HTTPException(
            status_code=422,
            detail=f"File too large: max {max_size} bytes",
        )

    package = await get_driver(db, driver_id)

    sha256 = hashlib.sha256(file_data).hexdigest()
    storage_key = f"{driver_id}/{filename}"
    old_storage_key = package.storage_key
    # Read before anything is stored: a package refused for its size (issue #1180)
    # must leave the stored file and the row as they were.
    replacement_metadata = _parse_driver_metadata(filename, file_data)

    # Upload new file first, then delete old one (create-then-delete)
    upload_object(storage_key, file_data)

    if old_storage_key != storage_key:
        try:
            delete_object(old_storage_key)
        except Exception:
            pass

    package.filename = filename
    package.storage_key = storage_key
    package.size_bytes = len(file_data)
    package.sha256 = sha256
    package.uploaded_by = username
    package.supports_dry_run = bool(replacement_metadata.get("supports_dry_run", False))
    package.supports_vrf = bool(replacement_metadata.get("supports_vrf", False))
    await db.commit()
    await db.refresh(package)
    return package


async def download_driver(db: AsyncSession, driver_id: uuid.UUID) -> tuple[DriverPackage, bytes]:
    package = await get_driver(db, driver_id)
    data = download_object(package.storage_key)
    return package, data


async def delete_driver(db: AsyncSession, driver_id: uuid.UUID) -> None:
    package = await get_driver(db, driver_id)

    # Check if any templates reference this driver
    count_result = await db.execute(
        select(func.count())
        .select_from(DeviceTemplate)
        .where(DeviceTemplate.driver_id == driver_id)
    )
    ref_count = count_result.scalar()
    if ref_count and ref_count > 0:
        raise HTTPException(
            status_code=409,
            detail="Cannot delete driver: templates still reference it",
        )

    try:
        delete_object(package.storage_key)
    except Exception:
        pass
    await db.delete(package)
    await db.commit()
