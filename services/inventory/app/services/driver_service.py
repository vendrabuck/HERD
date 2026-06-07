import hashlib
import io
import json
import logging
import tarfile
import uuid
import zipfile

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.driver_package import VALID_CONNECTION_TYPES, DriverPackage
from app.models.template import DeviceTemplate
from app.storage import delete_object, download_object, upload_object

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {".zip", ".tar.gz"}


def _parse_supports_dry_run(filename: str, file_data: bytes) -> bool:
    """Peek inside a driver package for `driver_metadata.json` and return its
    `supports_dry_run` claim.

    Returns False on any failure: unrecognized archive, malformed JSON,
    missing field, missing metadata file, or anything else surprising. The
    flag is opt-in and closed-by-default; only drivers that explicitly
    declare `supports_dry_run: true` are eligible for dry-run scheduling.
    """
    lower = filename.lower()
    try:
        if lower.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(file_data)) as zf:
                try:
                    raw = zf.read("driver_metadata.json")
                except KeyError:
                    return False
        elif lower.endswith(".tar.gz") or lower.endswith(".tgz"):
            with tarfile.open(fileobj=io.BytesIO(file_data), mode="r:gz") as tf:
                try:
                    member = tf.getmember("driver_metadata.json")
                except KeyError:
                    return False
                extracted = tf.extractfile(member)
                if extracted is None:
                    return False
                raw = extracted.read()
        else:
            return False
        meta = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        logger.info("driver upload: could not parse driver_metadata.json (%s)", exc)
        return False
    if not isinstance(meta, dict):
        return False
    return bool(meta.get("supports_dry_run", False))


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
    supports_dry_run = _parse_supports_dry_run(filename, file_data)

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
        supports_dry_run=supports_dry_run,
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
    package.supports_dry_run = _parse_supports_dry_run(filename, file_data)
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
