"""Driver package loader: download, extract, validate, and cache driver packages."""

import importlib.util
import json
import logging
import shutil
import tarfile
import uuid
import zipfile
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.driver_cache import DriverCache

DEFAULT_DRIVER_METADATA: dict = {"supports_dry_run": False}

logger = logging.getLogger(__name__)

# Required methods per connection type
REQUIRED_METHODS = {
    "Layer 1 Switch": ["login", "logout", "connect_ports", "disconnect_ports", "status"],
    "Layer 2 Switch": [
        "login",
        "logout",
        "create_vlan",
        "add_to_vlan",
        "remove_from_vlan",
        "delete_vlan",
        "status",
    ],
    "Layer 3 Switch": ["login", "logout", "configure_route", "remove_route", "status"],
    "Management": ["login", "logout", "configure", "backup", "status"],
}


async def get_cached_driver(
    db: AsyncSession,
    driver_id: uuid.UUID,
    expected_sha256: str,
) -> str | None:
    """Return the local path if the driver is cached with matching sha256, else None."""
    result = await db.execute(select(DriverCache).where(DriverCache.driver_id == driver_id))
    cache_entry = result.scalar_one_or_none()
    if cache_entry is None:
        return None
    if cache_entry.sha256 != expected_sha256:
        # Cache is stale; remove it
        old_path = Path(cache_entry.local_path)
        if old_path.exists():
            shutil.rmtree(old_path, ignore_errors=True)
        await db.delete(cache_entry)
        await db.commit()
        return None
    # Verify the path still exists on disk
    if not Path(cache_entry.local_path).exists():
        await db.delete(cache_entry)
        await db.commit()
        return None
    return cache_entry.local_path


async def download_driver_package(driver_id: uuid.UUID) -> bytes:
    """Download driver package from the inventory service."""
    url = f"{settings.inventory_service_url}/drivers/{driver_id}/internal-download"
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            url,
            headers={"X-Internal-Token": settings.internal_api_token},
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.content


def extract_driver_package(package_bytes: bytes, filename: str, dest_dir: Path) -> None:
    """Extract a .zip or .tar.gz driver package into dest_dir."""
    dest_dir.mkdir(parents=True, exist_ok=True)

    if filename.endswith(".zip"):
        import io

        with zipfile.ZipFile(io.BytesIO(package_bytes)) as zf:
            zf.extractall(dest_dir)
    elif filename.endswith(".tar.gz") or filename.endswith(".tgz"):
        import io

        with tarfile.open(fileobj=io.BytesIO(package_bytes), mode="r:gz") as tf:
            tf.extractall(dest_dir, filter="data")
    else:
        raise ValueError(f"Unsupported package format: {filename}")


def validate_driver(driver_dir: Path, connection_type: str) -> list[str]:
    """Validate that the driver has driver.py with the required Driver class and methods.

    Returns a list of validation errors (empty if valid).
    """
    errors = []
    driver_py = driver_dir / "driver.py"
    if not driver_py.exists():
        errors.append("Missing driver.py at package root")
        return errors

    # Load the module to inspect it
    try:
        spec = importlib.util.spec_from_file_location("driver_check", driver_py)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as e:
        errors.append(f"Failed to load driver.py: {e}")
        return errors

    if not hasattr(module, "Driver"):
        errors.append("driver.py must define a class named Driver")
        return errors

    driver_cls = module.Driver
    required = REQUIRED_METHODS.get(connection_type)
    if required is None:
        errors.append(f"Unknown connection type: {connection_type}")
        return errors

    for method_name in required:
        if not callable(getattr(driver_cls, method_name, None)):
            errors.append(f"Driver class is missing required method: {method_name}")

    return errors


def read_driver_metadata(driver_dir: Path) -> dict:
    """Read driver_metadata.json from the package root.

    Returns DEFAULT_DRIVER_METADATA if the file is missing or malformed.
    The contract is opt-in: drivers must explicitly declare
    `supports_dry_run: true` to be eligible for AI-initiated dry-run apply
    scheduling. See docs/DRIVERS.md.
    """
    metadata_path = driver_dir / "driver_metadata.json"
    if not metadata_path.exists():
        return dict(DEFAULT_DRIVER_METADATA)
    try:
        with metadata_path.open(encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            logger.warning("driver_metadata.json is not an object: %s", metadata_path)
            return dict(DEFAULT_DRIVER_METADATA)
        return data
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read driver_metadata.json at %s: %s", metadata_path, exc)
        return dict(DEFAULT_DRIVER_METADATA)


async def get_driver_metadata(db: AsyncSession, driver_id: uuid.UUID) -> dict:
    """Return the cached driver_metadata.json for a driver_id.

    Reads `DriverCache.metadata_json`. Returns DEFAULT_DRIVER_METADATA if the
    driver is not yet cached or the cached row has no metadata column set.
    Callers that need the freshest metadata must call `load_driver` first
    (which populates the cache as a side effect).
    """
    result = await db.execute(select(DriverCache).where(DriverCache.driver_id == driver_id))
    entry = result.scalar_one_or_none()
    if entry is None or not entry.metadata_json:
        return dict(DEFAULT_DRIVER_METADATA)
    try:
        data = json.loads(entry.metadata_json)
        if not isinstance(data, dict):
            return dict(DEFAULT_DRIVER_METADATA)
        return data
    except json.JSONDecodeError:
        return dict(DEFAULT_DRIVER_METADATA)


async def load_driver(
    db: AsyncSession,
    driver_id: uuid.UUID,
    driver_sha256: str,
    driver_filename: str,
    connection_type: str,
) -> str:
    """Load a driver package: check cache, download if needed, extract, validate, cache.

    Returns the local path to the extracted driver directory.
    Raises ValueError on validation failure, RuntimeError on download/extraction failure.
    """
    # Check cache first
    cached_path = await get_cached_driver(db, driver_id, driver_sha256)
    if cached_path:
        logger.info("Driver cache hit", extra={"driver_id": str(driver_id)})
        return cached_path

    logger.info("Driver cache miss, downloading", extra={"driver_id": str(driver_id)})

    # Download
    try:
        package_bytes = await download_driver_package(driver_id)
    except Exception as e:
        raise RuntimeError(f"Failed to download driver {driver_id}: {e}") from e

    # Extract
    dest_dir = Path(settings.driver_cache_path) / str(driver_id)
    try:
        extract_driver_package(package_bytes, driver_filename, dest_dir)
    except Exception as e:
        shutil.rmtree(dest_dir, ignore_errors=True)
        raise RuntimeError(f"Failed to extract driver {driver_id}: {e}") from e

    # Validate
    errors = validate_driver(dest_dir, connection_type)
    if errors:
        shutil.rmtree(dest_dir, ignore_errors=True)
        raise ValueError(f"Driver validation failed: {'; '.join(errors)}")

    # Capture driver_metadata.json (opt-in capability declaration). Default-shape
    # if missing so the cache always reflects "we looked and this is what we saw".
    metadata = read_driver_metadata(dest_dir)
    metadata_json = json.dumps(metadata)

    # Update cache
    result = await db.execute(select(DriverCache).where(DriverCache.driver_id == driver_id))
    existing = result.scalar_one_or_none()
    if existing:
        existing.sha256 = driver_sha256
        existing.local_path = str(dest_dir)
        existing.metadata_json = metadata_json
    else:
        cache_entry = DriverCache(
            driver_id=driver_id,
            sha256=driver_sha256,
            local_path=str(dest_dir),
            metadata_json=metadata_json,
        )
        db.add(cache_entry)
    await db.commit()

    logger.info(
        "Driver loaded and cached",
        extra={"driver_id": str(driver_id), "path": str(dest_dir)},
    )
    return str(dest_dir)
