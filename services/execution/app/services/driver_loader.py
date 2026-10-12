"""Driver package loader: download, extract, validate, and cache driver packages."""

import gzip
import io
import json
import logging
import shutil
import tarfile
import uuid
import zipfile
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.driver_cache import DriverCache
from app.services.driver_structure import REQUIRED_METHODS as REQUIRED_METHODS
from app.services.driver_structure import DriverSourceError, driver_structure_errors

# Capability declarations are opt-in and closed by default: a package whose
# driver_metadata.json is missing, unreadable, or silent on a flag declares
# nothing. `supports_vrf` (ADR 0014 addendum X-G, issue #755) is load-bearing
# here, because every shipped Layer 3 signature ends in `**_`: a driver that
# has not declared VRF support would silently swallow a `virtual_router`
# keyword, install the route in the default table, and report success.
DEFAULT_DRIVER_METADATA: dict = {"supports_dry_run": False, "supports_vrf": False}

logger = logging.getLogger(__name__)


class DriverPackageError(Exception):
    """A driver package can never load as-is; retry cannot fix it.

    Raised for structural defects that are fixed for a given SHA256 and so
    reproduce identically on every redelivery: a structurally invalid archive
    (unsupported format, corrupt zip/tar, over the extraction ceilings), a
    missing driver.py, a driver.py that does not parse (any failure in
    driver_structure.PARSE_FAILURES, the parser's RecursionError and MemoryError
    included, issue #1178), a missing Driver class, an unknown connection type,
    or a Driver missing a method the connection type requires.
    Distinct from a download failure (inventory unreachable), which stays a
    transient RuntimeError. The dynamic-provisioning consumer maps this to a
    PermanentEventError so a broken recipe dead-letters on first delivery
    instead of NAK'ing through the full retry ladder (issue #279). Only what the
    load checks counts: the load parses driver.py and never imports it (issue
    #1114), so a recipe whose top-level code raises (an import of a missing
    module) loads, fails its first sandboxed call (login), and that event is
    NAK'd like any failed login and dead-lettered only when NATS_MAX_DELIVER
    deliveries are spent (``nats_dlq_exhausted``), not on its first delivery.
    """


def is_permanent_load_failure(exc: BaseException) -> bool:
    """True when a ``load_driver`` failure can never succeed on a retry (issue #1002).

    The one classifier for load failures. Only a ``DriverPackageError`` is
    permanent: the package is broken for its SHA256 and reproduces identically.
    Everything else ``load_driver`` can raise (the download ``RuntimeError`` when
    inventory or package storage is unreachable, a cache-table read error) is
    transient, and a caller must leave the work retryable.
    """
    return isinstance(exc, DriverPackageError)


def driver_load_failure_text(exc: BaseException) -> str:
    """The fixed, sanitized text stored for a ``load_driver`` failure (issues #840, #887).

    ``driver load failed: <ClassName>``, the class being the wrapped cause's when
    the exception chains one (the download's httpx error, the extract's
    zipfile/OSError) and the exception's own otherwise. Never ``str(exc)``: a
    row or an API response carries only this HERD-authored text, and the caller
    logs the full text in a log message.
    """
    cause = exc.__cause__
    cause_class = type(cause).__name__ if cause is not None else type(exc).__name__
    return f"driver load failed: {cause_class}"


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


# Fixed ceilings on what extracting one driver package may write (issue #1115). The
# upload is capped compressed (inventory's DRIVER_MAX_SIZE_BYTES), but a highly
# compressible archive under that cap could still expand to fill the driver cache
# volume every load shares. Real packages are a few files and kilobytes. Constants by
# decision, not settings.
MAX_EXTRACTED_BYTES = 100 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 10_000
# The largest single read the tar reader may make from the decompressed stream (issue
# #1179). tarfile reads member headers in 512-byte blocks and copies member data in
# 16 KiB chunks, but reads a pax extended header (types x and g) or a GNU long name
# or long link (types L and K) whole, in one call, before the member it describes
# exists to be checked against the ceilings above. Bounding the read bounds that
# allocation. A real pax header or long name is well under a kilobyte.
MAX_TAR_READ_BYTES = 1024 * 1024


class PackageLimitError(ValueError):
    """A driver archive declares more entries or more uncompressed bytes than the ceilings.

    The message is HERD-authored and names the ceiling that was exceeded. load_driver
    turns it into a DriverPackageError like any other extraction failure, so the row
    text is ``driver load failed: PackageLimitError``.
    """


def _check_archive_limits(entries: int, total_bytes: int) -> None:
    if entries > MAX_ARCHIVE_ENTRIES:
        raise PackageLimitError(
            f"Driver package has more than {MAX_ARCHIVE_ENTRIES} entries; nothing was extracted"
        )
    if total_bytes > MAX_EXTRACTED_BYTES:
        raise PackageLimitError(
            f"Driver package declares more than {MAX_EXTRACTED_BYTES} bytes uncompressed; "
            "nothing was extracted"
        )


class _BoundedTarStream:
    """The decompressed tar stream, refusing any single read over MAX_TAR_READ_BYTES.

    tarfile reads a pax or GNU long-name header's whole declared size in one call;
    this refuses that call before anything is decompressed into memory for it.
    Every other read tarfile makes is a 512-byte header block or a data chunk of at
    most its 16 KiB copy buffer, so a well-formed package never reaches the bound.
    """

    def __init__(self, raw: gzip.GzipFile) -> None:
        self._raw = raw

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0 or size > MAX_TAR_READ_BYTES:
            raise PackageLimitError(
                f"Driver package has a tar header record over {MAX_TAR_READ_BYTES} bytes "
                "(a pax extended header or GNU long name); nothing was extracted"
            )
        return self._raw.read(size)

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        return self._raw.seek(offset, whence)

    def tell(self) -> int:
        return self._raw.tell()


def extract_driver_package(package_bytes: bytes, filename: str, dest_dir: Path) -> None:
    """Extract a .zip or .tar.gz driver package into dest_dir.

    The archive's entry count and declared uncompressed total are checked against
    MAX_ARCHIVE_ENTRIES and MAX_EXTRACTED_BYTES before anything is written, dest_dir
    included (issue #1115). A declared size also bounds what extraction writes: zipfile
    reads no more than an entry's ``file_size`` and tarfile writes exactly a member's
    ``size``.

    A tar is read through ``_BoundedTarStream``, so no single read from the
    decompressed stream exceeds MAX_TAR_READ_BYTES: a pax or GNU long-name header
    declaring more is refused before it is read into memory (issue #1179). Member
    headers are read one at a time and the scan stops at the first one past a
    ceiling; skipping over member data still decompresses it in small chunks, and
    extraction then decompresses the stream a second time from the start. The work
    is synchronous, and load_driver calls it on the event loop (a limit: up to about
    twice MAX_EXTRACTED_BYTES of gunzip per cache miss blocks the loop).
    """
    if filename.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(package_bytes)) as zf:
            infos = zf.infolist()
            _check_archive_limits(len(infos), sum(info.file_size for info in infos))
            dest_dir.mkdir(parents=True, exist_ok=True)
            zf.extractall(dest_dir)
    elif filename.endswith(".tar.gz") or filename.endswith(".tgz"):
        with gzip.GzipFile(fileobj=io.BytesIO(package_bytes), mode="rb") as raw:
            try:
                tf = tarfile.open(fileobj=_BoundedTarStream(raw), mode="r:")
            except OSError as exc:
                # What tarfile.open(mode="r:gz") raises for a stream that is not gzip.
                raise tarfile.ReadError("not a gzip file") from exc
            with tf:
                entries = 0
                total_bytes = 0
                for member in tf:
                    entries += 1
                    total_bytes += member.size
                    _check_archive_limits(entries, total_bytes)
                dest_dir.mkdir(parents=True, exist_ok=True)
                tf.extractall(dest_dir, filter="data")
    else:
        raise ValueError(f"Unsupported package format: {filename}")


def validate_driver(driver_dir: Path, connection_type: str) -> list[str]:
    """Validate that the driver has driver.py with the required Driver class and methods.

    Checked by parsing driver.py, never by importing it, through the one structural
    check the package validator also uses (``driver_structure_errors``, issue #1114):
    driver package code runs only inside the driver sandbox. Returns a list of
    validation errors (empty if valid).
    """
    try:
        return driver_structure_errors(driver_dir, connection_type)
    except DriverSourceError as exc:
        cause = exc.__cause__
        # The parser's text can carry a path or a fragment of the source, so only
        # the class name is kept in the errors list, which load_driver folds into
        # a DriverPackageError that is stored on the run row (issues #840, #870).
        # The full text goes in the log MESSAGE, not `extra`: JSONFormatter's
        # key-name redaction cannot see inside a value.
        logger.error("Failed to parse driver.py at %s: %s", driver_dir / "driver.py", cause)
        return [f"Failed to load driver.py: {type(cause).__name__}"]


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


def extract_config_schema_json(driver_dir: Path) -> str | None:
    """Extract the driver-published config schema and return it JSON-encoded.

    Runs the `__config_schema__` sentinel in the sandbox (which reads
    Driver.config_schema() on the class object, never instantiating it). Returns
    the JSON-encoded schema dict, or None when the driver published no schema,
    returned a non-dict, or the extraction failed/timed out. This is fail-open
    by design: a broken config_schema() must never block a driver load; the
    validation path falls back to the registry. See issue #23.

    The subprocess keeps driver code out of this process: `validate_driver`
    checks the package by parsing driver.py, never importing it (issue #1114),
    so this sandboxed call is the first time any of the package's code runs on
    the load path. It also bounds the config_schema() call itself: it runs with
    resource limits and a timeout, so a runaway config_schema() cannot hang the
    execution service.
    """
    # Imported lazily so unit tests that exercise extract/validate without the
    # sandbox do not pull in the subprocess machinery at import time.
    from app.services.driver_sandbox import extract_config_schema

    try:
        result = extract_config_schema(str(driver_dir))
    except Exception as exc:  # defensive: the wrapper should not raise
        logger.warning("Config-schema extraction raised for %s: %s", driver_dir, exc)
        return None
    if not result.get("success"):
        logger.warning(
            "Config-schema extraction failed for %s: %s",
            driver_dir,
            result.get("error"),
        )
        return None
    output = result.get("output") or {}
    # has_schema signals that config_schema() exists and returned a dict.
    if not output.get("has_schema"):
        return None
    schema = output.get("schema")
    if not isinstance(schema, dict):
        logger.warning(
            "Driver at %s published a non-dict config schema (%s); ignoring",
            driver_dir,
            type(schema).__name__,
        )
        return None
    return json.dumps(schema)


async def get_driver_config_schema(db: AsyncSession, driver_id: uuid.UUID) -> dict | None:
    """Return the cached driver-published config schema for a driver_id, or None.

    Reads `DriverCache.config_schema_json`. Returns None if the driver is not
    cached, published no schema, or the cached value is malformed. Callers that
    need the freshest schema must call `load_driver` first (which populates the
    cache as a side effect). None signals "no published schema; use the
    registry".
    """
    result = await db.execute(select(DriverCache).where(DriverCache.driver_id == driver_id))
    entry = result.scalar_one_or_none()
    if entry is None or not entry.config_schema_json:
        return None
    try:
        data = json.loads(entry.config_schema_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data


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
    Raises RuntimeError on a download failure (transient: inventory unreachable),
    DriverPackageError on a structurally invalid archive or a validation failure
    (permanent: the package can never load as-is).
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
        # e is a foreign (httpx) exception and can carry the inventory service's
        # internal URL; only the class name is kept in the raised message, which
        # run_driver_action stores verbatim on the run row (issue #870). The full
        # text goes in the log MESSAGE, not `extra`: the text is foreign and may carry a secret or
        # internal URL inside a value, which JSONFormatter's key-name redaction
        # cannot see.
        logger.error("Failed to download driver %s: %s", driver_id, e)
        raise RuntimeError(f"Failed to download driver {driver_id}: {type(e).__name__}") from e

    # Extract into a directory of this attempt's own (issue #1097). Two first
    # loads of one driver used to extract into the same <cache>/<driver id>
    # directory at once, so one could read a half-written tree or remove the
    # other's files on a validation failure. A per-attempt directory is never
    # shared, and only the cache row (written below, after validation) makes it
    # visible to a later load. A sibling of the legacy <cache>/<driver id> path,
    # never a child, so removing a stale legacy directory cannot remove it.
    dest_dir = Path(settings.driver_cache_path) / f"{driver_id}-{uuid.uuid4().hex}"
    try:
        extract_driver_package(package_bytes, driver_filename, dest_dir)
    except Exception as e:
        # e is a foreign (zipfile/tarfile/OSError) exception and can carry local
        # filesystem paths; same class-name-only treatment as the download above.
        logger.error("Failed to extract driver %s: %s", driver_id, e)
        shutil.rmtree(dest_dir, ignore_errors=True)
        raise DriverPackageError(f"Failed to extract driver {driver_id}: {type(e).__name__}") from e

    # Until the cache row names it, nothing else will ever remove this attempt's
    # directory, so any failure while the package is checked removes it here: a
    # check that returns errors, and one that raises (issue #1178). A raise is
    # re-raised unchanged, so its transient or permanent class is kept.
    try:
        # Validate
        errors = validate_driver(dest_dir, connection_type)
        if errors:
            raise DriverPackageError(f"Driver validation failed: {'; '.join(errors)}")

        # Capture driver_metadata.json (opt-in capability declaration). Default-shape
        # if missing so the cache always reflects "we looked and this is what we saw".
        metadata = read_driver_metadata(dest_dir)
        metadata_json = json.dumps(metadata)

        # Capture the driver-published config schema (issue #23). Fail-open: a
        # driver that omits config_schema(), ships a broken one, or times out
        # yields None and we degrade to the registry at validation time.
        config_schema_json = extract_config_schema_json(dest_dir)
    except BaseException:
        shutil.rmtree(dest_dir, ignore_errors=True)
        raise

    # Record the cache row (issue #1097): a real insert-or-nothing on the unique
    # driver_id, then a re-read, so a concurrent first load of the same driver
    # never fails on the unique constraint. The loser adopts the winner's row
    # when it holds the same package, and discards its own extraction.
    local_path = await _record_cache_row(
        db,
        driver_id=driver_id,
        driver_sha256=driver_sha256,
        dest_dir=dest_dir,
        metadata_json=metadata_json,
        config_schema_json=config_schema_json,
    )

    logger.info(
        "Driver loaded and cached",
        extra={"driver_id": str(driver_id), "path": local_path},
    )
    return local_path


def _insert_cache_row_if_absent(dialect_name: str, values: dict):
    """INSERT ... ON CONFLICT (driver_id) DO NOTHING for the session's dialect."""
    insert = pg_insert if dialect_name == "postgresql" else sqlite_insert
    return insert(DriverCache).values(**values).on_conflict_do_nothing(index_elements=["driver_id"])


async def _read_cache_row(db: AsyncSession, driver_id: uuid.UUID) -> DriverCache | None:
    # populate_existing: a row this session already holds in its identity map
    # would otherwise come back with the attributes it had before a concurrent
    # writer's commit.
    result = await db.execute(
        select(DriverCache)
        .where(DriverCache.driver_id == driver_id)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def _record_cache_row(
    db: AsyncSession,
    *,
    driver_id: uuid.UUID,
    driver_sha256: str,
    dest_dir: Path,
    metadata_json: str,
    config_schema_json: str | None,
) -> str:
    """Write the driver's cache row for a freshly extracted package; return the
    directory the caller should load from.

    - No row: insert ours with ON CONFLICT DO NOTHING, commit, and re-read. When
      the re-read row is ours, we won.
    - A row for the SAME sha256 whose directory exists (a concurrent first load
      won the insert): adopt it and remove our own extraction, so one directory
      serves the package.
    - Any other row (a different sha256, or a directory that is gone): update it
      in place to ours, the behavior load_driver always had for a stale row.
    """
    ours = str(dest_dir)
    existing = await _read_cache_row(db, driver_id)
    if existing is None:
        await db.execute(
            _insert_cache_row_if_absent(
                db.get_bind().dialect.name,
                {
                    "id": uuid.uuid4(),
                    "driver_id": driver_id,
                    "sha256": driver_sha256,
                    "local_path": ours,
                    "metadata_json": metadata_json,
                    "config_schema_json": config_schema_json,
                },
            )
        )
        await db.commit()
        existing = await _read_cache_row(db, driver_id)
        if existing is None or existing.local_path == ours:
            # existing is None only if a concurrent stale-row eviction removed the
            # winner's row between our insert and the re-read; our extraction is
            # valid, so serve it (the next load re-caches).
            return ours

    if existing.sha256 == driver_sha256 and Path(existing.local_path).exists():
        logger.info(
            "Driver cache row written by a concurrent load; adopting it",
            extra={"driver_id": str(driver_id), "path": existing.local_path},
        )
        shutil.rmtree(dest_dir, ignore_errors=True)
        return existing.local_path

    replaced_path = existing.local_path
    existing.sha256 = driver_sha256
    existing.local_path = ours
    existing.metadata_json = metadata_json
    existing.config_schema_json = config_schema_json
    await db.commit()
    if replaced_path != ours:
        # The replaced directory held another package (or is already gone);
        # get_cached_driver removes a stale directory the same way.
        shutil.rmtree(replaced_path, ignore_errors=True)
    return ours
