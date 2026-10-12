"""Tests for driver_loader.py: load_driver full flow and edge cases."""

import io
import tempfile
import uuid
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from app.database import Base
from app.models.driver_cache import DriverCache
from app.services.driver_loader import (
    DriverPackageError,
    download_driver_package,
    extract_config_schema_json,
    extract_driver_package,
    get_driver_config_schema,
    get_driver_metadata,
    load_driver,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

test_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TestSessionLocal = async_sessionmaker(test_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def setup_db():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def db():
    async with TestSessionLocal() as session:
        yield session


VALID_L1_DRIVER = """
class Driver:
    def __init__(self, context):
        self.context = context
    def login(self): return {"success": True}
    def logout(self): return {"success": True}
    def connect_ports(self, a, b): return {"success": True}
    def disconnect_ports(self, a, b): return {"success": True}
    def status(self): return {"reachable": True}
"""


MGMT_DRIVER_WITH_SCHEMA = """
class Driver:
    def __init__(self, context):
        self.context = context
    @classmethod
    def config_schema(cls):
        return {
            "type": "object",
            "properties": {"hostname": {"type": "string", "maxLength": 64}},
            "additionalProperties": False,
        }
    def login(self): return {"success": True}
    def logout(self): return {"success": True}
    def configure(self, **kwargs): return {"success": True}
    def backup(self): return {"success": True}
    def status(self): return {"reachable": True}
"""

MGMT_DRIVER_NO_SCHEMA = """
class Driver:
    def __init__(self, context):
        self.context = context
    def login(self): return {"success": True}
    def logout(self): return {"success": True}
    def configure(self, **kwargs): return {"success": True}
    def backup(self): return {"success": True}
    def status(self): return {"reachable": True}
"""

MGMT_DRIVER_BROKEN_SCHEMA = """
class Driver:
    def __init__(self, context):
        self.context = context
    @classmethod
    def config_schema(cls):
        raise RuntimeError("schema generation blew up")
    def login(self): return {"success": True}
    def logout(self): return {"success": True}
    def configure(self, **kwargs): return {"success": True}
    def backup(self): return {"success": True}
    def status(self): return {"reachable": True}
"""


def _make_zip(driver_code: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("driver.py", driver_code)
    return buf.getvalue()


# --- extract_driver_package edge cases ---


def test_extract_unsupported_format():
    with tempfile.TemporaryDirectory() as tmpdir:
        dest = Path(tmpdir) / "driver"
        with pytest.raises(ValueError, match="Unsupported package format"):
            extract_driver_package(b"data", "driver.exe", dest)


# --- download_driver_package ---


@pytest.mark.asyncio
async def test_download_driver_package_success():
    from unittest.mock import MagicMock

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = b"zipdata"
    mock_resp.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.services.driver_loader.httpx.AsyncClient", return_value=mock_client):
        result = await download_driver_package(uuid.uuid4())
    assert result == b"zipdata"


@pytest.mark.asyncio
async def test_download_driver_package_failure():
    from unittest.mock import MagicMock

    mock_resp = MagicMock()
    mock_resp.raise_for_status.side_effect = Exception("404 Not Found")

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_resp
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.services.driver_loader.httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(Exception, match="404"):
            await download_driver_package(uuid.uuid4())


# --- load_driver full flow ---


@pytest.mark.asyncio
async def test_load_driver_cache_hit(db):
    """Cache hit returns cached path without download."""
    driver_id = uuid.uuid4()
    with tempfile.TemporaryDirectory() as tmpdir:
        # Write a valid driver
        (Path(tmpdir) / "driver.py").write_text(VALID_L1_DRIVER)
        cache = DriverCache(driver_id=driver_id, sha256="abc", local_path=tmpdir)
        db.add(cache)
        await db.commit()

        result = await load_driver(db, driver_id, "abc", "driver.zip", "Layer 1 Switch")
        assert result == tmpdir


@pytest.mark.asyncio
async def test_load_driver_download_and_cache(db):
    """Cache miss downloads, extracts, validates, and caches."""
    driver_id = uuid.uuid4()
    zip_bytes = _make_zip(VALID_L1_DRIVER)

    with tempfile.TemporaryDirectory() as cache_root:
        with (
            patch(
                "app.services.driver_loader.download_driver_package",
                new=AsyncMock(return_value=zip_bytes),
            ),
            patch("app.services.driver_loader.settings") as mock_settings,
        ):
            mock_settings.driver_cache_path = cache_root
            mock_settings.inventory_service_url = "http://test"
            mock_settings.internal_api_token = "token"
            result = await load_driver(db, driver_id, "sha256", "driver.zip", "Layer 1 Switch")
        assert Path(result).exists()
        assert (Path(result) / "driver.py").exists()

        # Verify cache entry was created
        check = await db.execute(select(DriverCache).where(DriverCache.driver_id == driver_id))
        assert check.scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_load_driver_download_failure(db):
    with (
        patch(
            "app.services.driver_loader.download_driver_package",
            new=AsyncMock(side_effect=Exception("Connection refused")),
        ),
    ):
        with pytest.raises(RuntimeError, match="Failed to download"):
            await load_driver(db, uuid.uuid4(), "sha", "d.zip", "Layer 1 Switch")


@pytest.mark.asyncio
async def test_load_driver_extraction_failure(db):
    with tempfile.TemporaryDirectory() as cache_root:
        with (
            patch(
                "app.services.driver_loader.download_driver_package",
                new=AsyncMock(return_value=b"not a zip"),
            ),
            patch("app.services.driver_loader.settings") as mock_settings,
        ):
            mock_settings.driver_cache_path = cache_root
            mock_settings.inventory_service_url = "http://test"
            mock_settings.internal_api_token = "token"
            # A structurally invalid archive is a permanent package error, not a
            # transient RuntimeError (issue #279).
            with pytest.raises(DriverPackageError, match="Failed to extract"):
                await load_driver(db, uuid.uuid4(), "sha", "driver.zip", "Layer 1 Switch")


@pytest.mark.asyncio
async def test_load_driver_validation_failure(db):
    """Driver zip without required methods raises DriverPackageError (issue #279)."""
    bad_driver = """
class Driver:
    def __init__(self, context): pass
    def login(self): pass
"""
    zip_bytes = _make_zip(bad_driver)

    with tempfile.TemporaryDirectory() as cache_root:
        with (
            patch(
                "app.services.driver_loader.download_driver_package",
                new=AsyncMock(return_value=zip_bytes),
            ),
            patch("app.services.driver_loader.settings") as mock_settings,
        ):
            mock_settings.driver_cache_path = cache_root
            mock_settings.inventory_service_url = "http://test"
            mock_settings.internal_api_token = "token"
            with pytest.raises(DriverPackageError, match="validation failed"):
                await load_driver(db, uuid.uuid4(), "sha", "driver.zip", "Layer 1 Switch")


# --- Foreign exception text never reaches the raised message (issue #870) --
#
# load_driver's three raise sites that can embed a FOREIGN exception's text
# (download: httpx; extract: zipfile/tarfile/OSError; validate: whatever
# driver.py's own import raised) must keep only the exception's class name in
# the message that ultimately lands on the run row (execution_service.py
# stores str(e) verbatim on the driver-LOAD except branch). The full text
# still goes to the service log, which run_driver_action.py's precedent
# insists on checking via a real formatted JSONFormatter record, not just
# caplog's raw LogRecord attributes (extra-key dropping is invisible to
# caplog).

_SENTINEL = "http://secret-internal-host/leak"


@pytest.mark.asyncio
async def test_load_driver_download_failure_sanitizes_foreign_text(db, caplog):
    with (
        patch(
            "app.services.driver_loader.download_driver_package",
            new=AsyncMock(side_effect=Exception(f"boom at {_SENTINEL}")),
        ),
        caplog.at_level("ERROR"),
    ):
        with pytest.raises(RuntimeError) as exc:
            await load_driver(db, uuid.uuid4(), "sha", "d.zip", "Layer 1 Switch")

    message = str(exc.value)
    assert message.endswith("Exception")
    assert _SENTINEL not in message

    from herd_common.logging import JSONFormatter

    formatted = [JSONFormatter("execution").format(r) for r in caplog.records]
    assert any(_SENTINEL in line for line in formatted)


@pytest.mark.asyncio
async def test_load_driver_extraction_failure_sanitizes_foreign_text(db, caplog):
    with tempfile.TemporaryDirectory() as cache_root:
        with (
            patch(
                "app.services.driver_loader.download_driver_package",
                new=AsyncMock(return_value=b"not a zip"),
            ),
            patch("app.services.driver_loader.settings") as mock_settings,
            patch(
                "app.services.driver_loader.extract_driver_package",
                side_effect=zipfile.BadZipFile(f"boom at {_SENTINEL}"),
            ),
            caplog.at_level("ERROR"),
        ):
            mock_settings.driver_cache_path = cache_root
            mock_settings.inventory_service_url = "http://test"
            mock_settings.internal_api_token = "token"
            with pytest.raises(DriverPackageError) as exc:
                await load_driver(db, uuid.uuid4(), "sha", "driver.zip", "Layer 1 Switch")

    message = str(exc.value)
    assert message.endswith("BadZipFile")
    assert _SENTINEL not in message

    from herd_common.logging import JSONFormatter

    formatted = [JSONFormatter("execution").format(r) for r in caplog.records]
    assert any(_SENTINEL in line for line in formatted)


@pytest.mark.asyncio
async def test_load_driver_validate_parse_failure_sanitizes_foreign_text(db, caplog):
    """driver.py failing to parse must not leak the parser's text into the
    DriverPackageError message; validate_driver keeps only the class name.
    The parser is patched to carry the sentinel the way a real message could
    carry a container filesystem path or a fragment of the source."""
    zip_bytes = _make_zip(VALID_L1_DRIVER)

    with tempfile.TemporaryDirectory() as cache_root:
        with (
            patch(
                "app.services.driver_loader.download_driver_package",
                new=AsyncMock(return_value=zip_bytes),
            ),
            patch("app.services.driver_loader.settings") as mock_settings,
            patch(
                "app.services.driver_structure.ast.parse",
                side_effect=SyntaxError(_SENTINEL),
            ),
            caplog.at_level("ERROR"),
        ):
            mock_settings.driver_cache_path = cache_root
            mock_settings.inventory_service_url = "http://test"
            mock_settings.internal_api_token = "token"
            with pytest.raises(DriverPackageError) as exc:
                await load_driver(db, uuid.uuid4(), "sha", "driver.zip", "Layer 1 Switch")
            # The failed load's own directory is removed.
            assert list(Path(cache_root).iterdir()) == []

    message = str(exc.value)
    assert message == "Driver validation failed: Failed to load driver.py: SyntaxError"
    assert _SENTINEL not in message

    from herd_common.logging import JSONFormatter

    formatted = [JSONFormatter("execution").format(r) for r in caplog.records]
    assert any(_SENTINEL in line for line in formatted)


@pytest.mark.asyncio
async def test_load_driver_never_runs_driver_code_in_process(db, tmp_path):
    """load_driver validates by parsing driver.py, never by importing it
    (issue #1114): a top-level side effect does not run in the execution
    process, and a top-level raise does not fail the load. The sandboxed
    config-schema step is patched out so any execution would be in-process."""
    flag = tmp_path / "ran.flag"
    driver_code = (
        f'open(r"{flag}", "w").write("ran")\n'
        'raise RuntimeError("top-level code ran")\n' + VALID_L1_DRIVER
    )
    zip_bytes = _make_zip(driver_code)
    cache_root = tmp_path / "cache"

    with (
        patch(
            "app.services.driver_loader.download_driver_package",
            new=AsyncMock(return_value=zip_bytes),
        ),
        patch("app.services.driver_loader.settings") as mock_settings,
        patch("app.services.driver_loader.extract_config_schema_json", return_value=None),
    ):
        mock_settings.driver_cache_path = str(cache_root)
        mock_settings.inventory_service_url = "http://test"
        mock_settings.internal_api_token = "token"
        result = await load_driver(db, uuid.uuid4(), "sha", "driver.zip", "Layer 1 Switch")

    assert (Path(result) / "driver.py").exists()
    assert not flag.exists()


@pytest.mark.asyncio
async def test_load_driver_refuses_archive_over_the_entry_ceiling(db, tmp_path):
    """An archive over an extraction ceiling is a permanent package error
    naming the limit's class only, and nothing is left in the cache (issue #1115)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("driver.py", VALID_L1_DRIVER)
        zf.writestr("extra.txt", "x")
    driver_id = uuid.uuid4()
    cache_root = tmp_path / "cache"
    cache_root.mkdir()

    with (
        patch(
            "app.services.driver_loader.download_driver_package",
            new=AsyncMock(return_value=buf.getvalue()),
        ),
        patch("app.services.driver_loader.settings") as mock_settings,
        patch("app.services.driver_loader.MAX_ARCHIVE_ENTRIES", 1),
    ):
        mock_settings.driver_cache_path = str(cache_root)
        mock_settings.inventory_service_url = "http://test"
        mock_settings.internal_api_token = "token"
        with pytest.raises(DriverPackageError) as exc:
            await load_driver(db, driver_id, "sha", "driver.zip", "Layer 1 Switch")

    assert str(exc.value) == f"Failed to extract driver {driver_id}: PackageLimitError"
    assert list(cache_root.iterdir()) == []


# --- config_schema_json capture (issue #23) ---


@pytest.mark.asyncio
async def test_load_driver_persists_published_config_schema(db):
    """A Management driver shipping config_schema() has its schema cached and
    readable via get_driver_config_schema."""
    driver_id = uuid.uuid4()
    zip_bytes = _make_zip(MGMT_DRIVER_WITH_SCHEMA)

    with tempfile.TemporaryDirectory() as cache_root:
        with (
            patch(
                "app.services.driver_loader.download_driver_package",
                new=AsyncMock(return_value=zip_bytes),
            ),
            patch("app.services.driver_loader.settings") as mock_settings,
        ):
            mock_settings.driver_cache_path = cache_root
            mock_settings.inventory_service_url = "http://test"
            mock_settings.internal_api_token = "token"
            # status_check_timeout_seconds is read by the sandbox wrapper.
            mock_settings.status_check_timeout_seconds = 10
            mock_settings.execution_timeout_seconds = 30
            mock_settings.allow_driver_pip_install = False
            mock_settings.driver_rlimit_as_bytes = 0
            mock_settings.driver_rlimit_cpu_seconds = 0
            mock_settings.driver_rlimit_nofile = 0
            mock_settings.driver_rlimit_nproc = 0
            await load_driver(db, driver_id, "sha", "driver.zip", "Management")

    row = (
        await db.execute(select(DriverCache).where(DriverCache.driver_id == driver_id))
    ).scalar_one()
    assert row.config_schema_json is not None

    schema = await get_driver_config_schema(db, driver_id)
    assert schema is not None
    assert schema["properties"]["hostname"]["maxLength"] == 64


@pytest.mark.asyncio
async def test_load_driver_no_schema_leaves_column_null(db):
    """A Management driver without config_schema() caches NULL; the helper
    returns None so validation falls back to the registry."""
    driver_id = uuid.uuid4()
    zip_bytes = _make_zip(MGMT_DRIVER_NO_SCHEMA)

    with tempfile.TemporaryDirectory() as cache_root:
        with (
            patch(
                "app.services.driver_loader.download_driver_package",
                new=AsyncMock(return_value=zip_bytes),
            ),
            patch("app.services.driver_loader.settings") as mock_settings,
        ):
            mock_settings.driver_cache_path = cache_root
            mock_settings.inventory_service_url = "http://test"
            mock_settings.internal_api_token = "token"
            mock_settings.status_check_timeout_seconds = 10
            mock_settings.execution_timeout_seconds = 30
            mock_settings.allow_driver_pip_install = False
            mock_settings.driver_rlimit_as_bytes = 0
            mock_settings.driver_rlimit_cpu_seconds = 0
            mock_settings.driver_rlimit_nofile = 0
            mock_settings.driver_rlimit_nproc = 0
            await load_driver(db, driver_id, "sha", "driver.zip", "Management")

    row = (
        await db.execute(select(DriverCache).where(DriverCache.driver_id == driver_id))
    ).scalar_one()
    assert row.config_schema_json is None
    assert await get_driver_config_schema(db, driver_id) is None


@pytest.mark.asyncio
async def test_get_driver_config_schema_missing_driver_returns_none(db):
    assert await get_driver_config_schema(db, uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_load_driver_updates_existing_cache(db):
    """If cache entry exists with different sha, it gets updated on new download."""
    driver_id = uuid.uuid4()
    zip_bytes = _make_zip(VALID_L1_DRIVER)

    # Pre-insert stale cache entry (will be removed by get_cached_driver sha mismatch)
    cache = DriverCache(driver_id=driver_id, sha256="old_sha", local_path="/nonexistent")
    db.add(cache)
    await db.commit()

    with tempfile.TemporaryDirectory() as cache_root:
        with (
            patch(
                "app.services.driver_loader.download_driver_package",
                new=AsyncMock(return_value=zip_bytes),
            ),
            patch("app.services.driver_loader.settings") as mock_settings,
        ):
            mock_settings.driver_cache_path = cache_root
            mock_settings.inventory_service_url = "http://test"
            mock_settings.internal_api_token = "token"
            result = await load_driver(db, driver_id, "new_sha", "driver.zip", "Layer 1 Switch")
        assert Path(result).exists()


# --- config_schema extraction is fail-open: a broken schema never blocks a load ---


@pytest.mark.asyncio
async def test_load_driver_broken_config_schema_loads_and_stores_null(db):
    """The core invariant (issue #23): a driver whose config_schema() raises still
    loads successfully and caches config_schema_json as NULL, so validation falls
    back to the registry. The extraction failure is swallowed, never surfaced."""
    driver_id = uuid.uuid4()
    zip_bytes = _make_zip(MGMT_DRIVER_BROKEN_SCHEMA)

    with tempfile.TemporaryDirectory() as cache_root:
        with (
            patch(
                "app.services.driver_loader.download_driver_package",
                new=AsyncMock(return_value=zip_bytes),
            ),
            patch("app.services.driver_loader.settings") as mock_settings,
        ):
            mock_settings.driver_cache_path = cache_root
            mock_settings.inventory_service_url = "http://test"
            mock_settings.internal_api_token = "token"
            mock_settings.status_check_timeout_seconds = 10
            mock_settings.execution_timeout_seconds = 30
            mock_settings.allow_driver_pip_install = False
            mock_settings.driver_rlimit_as_bytes = 0
            mock_settings.driver_rlimit_cpu_seconds = 0
            mock_settings.driver_rlimit_nofile = 0
            mock_settings.driver_rlimit_nproc = 0
            # The load must succeed despite the broken schema.
            result = await load_driver(db, driver_id, "sha", "driver.zip", "Management")
        assert Path(result).exists()

    row = (
        await db.execute(select(DriverCache).where(DriverCache.driver_id == driver_id))
    ).scalar_one()
    assert row.config_schema_json is None
    assert await get_driver_config_schema(db, driver_id) is None


# --- extract_config_schema_json unit branches (fail-open) ---


def test_extract_config_schema_json_wrapper_exception_returns_none(caplog):
    """If the sandbox wrapper itself raises, extraction returns None (defensive)."""
    with patch(
        "app.services.driver_sandbox.extract_config_schema",
        side_effect=RuntimeError("sandbox spawn failed"),
    ):
        assert extract_config_schema_json(Path("/tmp/whatever")) is None
    assert any("Config-schema extraction raised" in rec.message for rec in caplog.records)


def test_extract_config_schema_json_failed_run_returns_none(caplog):
    """A non-success extraction result (e.g. timeout) returns None."""
    with patch(
        "app.services.driver_sandbox.extract_config_schema",
        return_value={"success": False, "error": "timed out", "output": None},
    ):
        assert extract_config_schema_json(Path("/tmp/whatever")) is None
    assert any("Config-schema extraction failed" in rec.message for rec in caplog.records)


def test_extract_config_schema_json_no_schema_returns_none():
    """A successful run reporting has_schema False returns None without warning."""
    with patch(
        "app.services.driver_sandbox.extract_config_schema",
        return_value={"success": True, "output": {"has_schema": False, "schema": None}},
    ):
        assert extract_config_schema_json(Path("/tmp/whatever")) is None


def test_extract_config_schema_json_non_dict_schema_returns_none(caplog):
    """A driver that publishes a non-dict schema is ignored (returns None)."""
    with patch(
        "app.services.driver_sandbox.extract_config_schema",
        return_value={
            "success": True,
            "output": {"has_schema": True, "schema": ["not", "a", "dict"]},
        },
    ):
        assert extract_config_schema_json(Path("/tmp/whatever")) is None
    assert any("non-dict config schema" in rec.message for rec in caplog.records)


def test_extract_config_schema_json_dict_schema_is_json_encoded():
    """A valid dict schema is returned JSON-encoded."""
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    with patch(
        "app.services.driver_sandbox.extract_config_schema",
        return_value={"success": True, "output": {"has_schema": True, "schema": schema}},
    ):
        out = extract_config_schema_json(Path("/tmp/whatever"))
    import json as _json

    assert _json.loads(out) == schema


# --- get_driver_config_schema / get_driver_metadata malformed-cache handling ---


@pytest.mark.asyncio
async def test_get_driver_config_schema_malformed_json_returns_none(db):
    """A cached row with non-JSON config_schema_json yields None, not an error."""
    driver_id = uuid.uuid4()
    db.add(
        DriverCache(
            driver_id=driver_id,
            sha256="s",
            local_path="/x",
            config_schema_json="{not valid json",
        )
    )
    await db.commit()
    assert await get_driver_config_schema(db, driver_id) is None


@pytest.mark.asyncio
async def test_get_driver_config_schema_non_dict_returns_none(db):
    """A cached config schema that decodes to a non-dict yields None."""
    driver_id = uuid.uuid4()
    db.add(
        DriverCache(
            driver_id=driver_id,
            sha256="s",
            local_path="/x",
            config_schema_json="[1, 2, 3]",
        )
    )
    await db.commit()
    assert await get_driver_config_schema(db, driver_id) is None


@pytest.mark.asyncio
async def test_get_driver_metadata_malformed_json_returns_default(db):
    """A cached row with non-JSON metadata_json falls back to the default shape."""
    from app.services.driver_loader import DEFAULT_DRIVER_METADATA

    driver_id = uuid.uuid4()
    db.add(
        DriverCache(
            driver_id=driver_id,
            sha256="s",
            local_path="/x",
            metadata_json="{bad json",
        )
    )
    await db.commit()
    assert await get_driver_metadata(db, driver_id) == dict(DEFAULT_DRIVER_METADATA)


@pytest.mark.asyncio
async def test_get_driver_metadata_non_dict_returns_default(db):
    """Cached metadata that decodes to a non-dict falls back to the default shape."""
    from app.services.driver_loader import DEFAULT_DRIVER_METADATA

    driver_id = uuid.uuid4()
    db.add(
        DriverCache(
            driver_id=driver_id,
            sha256="s",
            local_path="/x",
            metadata_json='"a string, not an object"',
        )
    )
    await db.commit()
    assert await get_driver_metadata(db, driver_id) == dict(DEFAULT_DRIVER_METADATA)


@pytest.mark.asyncio
async def test_load_driver_updates_existing_row_under_concurrent_insert(db):
    """The in-place update branch (load_driver: if existing) guards a race where a
    concurrent writer reinserts the cache row after get_cached_driver cleared it.
    We simulate that by stubbing get_cached_driver to a clean miss (no delete) while
    a row for this driver_id is already present, so the re-query at update time
    finds it and updates in place rather than inserting a duplicate."""
    driver_id = uuid.uuid4()
    zip_bytes = _make_zip(VALID_L1_DRIVER)

    # A pre-existing row for this driver_id (the "concurrent writer's" insert).
    db.add(
        DriverCache(
            driver_id=driver_id,
            sha256="stale_sha",
            local_path="/stale",
            metadata_json=None,
            config_schema_json=None,
        )
    )
    await db.commit()

    with tempfile.TemporaryDirectory() as cache_root:
        with (
            patch(
                "app.services.driver_loader.get_cached_driver",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "app.services.driver_loader.download_driver_package",
                new=AsyncMock(return_value=zip_bytes),
            ),
            patch("app.services.driver_loader.settings") as mock_settings,
        ):
            mock_settings.driver_cache_path = cache_root
            mock_settings.inventory_service_url = "http://test"
            mock_settings.internal_api_token = "token"
            mock_settings.status_check_timeout_seconds = 10
            mock_settings.execution_timeout_seconds = 30
            mock_settings.allow_driver_pip_install = False
            mock_settings.driver_rlimit_as_bytes = 0
            mock_settings.driver_rlimit_cpu_seconds = 0
            mock_settings.driver_rlimit_nofile = 0
            mock_settings.driver_rlimit_nproc = 0
            await load_driver(db, driver_id, "fresh_sha", "driver.zip", "Layer 1 Switch")

    rows = list(
        (await db.execute(select(DriverCache).where(DriverCache.driver_id == driver_id)))
        .scalars()
        .all()
    )
    # Exactly one row, updated in place (not a duplicate insert).
    assert len(rows) == 1
    assert rows[0].sha256 == "fresh_sha"
    # The attempt's own directory, a sibling of the legacy <cache>/<driver id> path.
    assert Path(rows[0].local_path).name.startswith(f"{driver_id}-")


# --- Concurrent first loads (issue #1097) ---


def _patch_loader_settings(mock_settings, cache_root: str) -> None:
    mock_settings.driver_cache_path = cache_root
    mock_settings.inventory_service_url = "http://test"
    mock_settings.internal_api_token = "token"


@pytest.mark.asyncio
async def test_concurrent_first_loads_of_one_driver_both_succeed(tmp_path):
    """Two first loads of one driver, both past the cache miss before either writes
    the cache row: both succeed, one cache row exists, both callers get the SAME
    directory, and the loser's extraction is removed. Before the fix the second
    insert broke the unique driver_id and raised IntegrityError."""
    import asyncio

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cache.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    driver_id = uuid.uuid4()
    zip_bytes = _make_zip(VALID_L1_DRIVER)
    cache_root = tmp_path / "cache"
    cache_root.mkdir()

    # Both loads must be past get_cached_driver (a miss) before either proceeds.
    arrived = 0
    both_missed = asyncio.Event()

    async def _download(_driver_id):
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            both_missed.set()
        await both_missed.wait()
        return zip_bytes

    async def _load():
        async with sessions() as session:
            return await load_driver(session, driver_id, "sha-1", "driver.zip", "Layer 1 Switch")

    try:
        with (
            patch(
                "app.services.driver_loader.download_driver_package",
                new=AsyncMock(side_effect=_download),
            ),
            patch("app.services.driver_loader.extract_config_schema_json", return_value=None),
            patch("app.services.driver_loader.settings") as mock_settings,
        ):
            _patch_loader_settings(mock_settings, str(cache_root))
            first, second = await asyncio.wait_for(asyncio.gather(_load(), _load()), 30)

        assert first == second
        assert Path(first).is_dir()
        assert (Path(first) / "driver.py").is_file()
        # Only the winner's extraction remains on disk.
        assert [p.name for p in cache_root.iterdir()] == [Path(first).name]
        async with sessions() as session:
            rows = (
                (
                    await session.execute(
                        select(DriverCache).where(DriverCache.driver_id == driver_id)
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 1
        assert rows[0].local_path == first
        assert rows[0].sha256 == "sha-1"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_load_driver_adopts_row_written_after_its_own_read(db, tmp_path):
    """The insert is insert-or-nothing: when a concurrent load commits the row
    between this load's read and its insert, the insert does nothing, the
    re-read finds the winner (same sha256, directory present), and this load
    returns the winner's directory and removes its own extraction."""
    from app.services import driver_loader

    driver_id = uuid.uuid4()
    winner_dir = tmp_path / f"{driver_id}-winner"
    winner_dir.mkdir()
    cache_root = tmp_path / "cache"
    cache_root.mkdir()

    real_read = driver_loader._read_cache_row
    calls = 0

    async def _read(session, did):
        nonlocal calls
        calls += 1
        if calls == 1:
            # The concurrent winner commits right after our first read saw nothing.
            session.add(
                DriverCache(
                    driver_id=driver_id,
                    sha256="sha-1",
                    local_path=str(winner_dir),
                    metadata_json=None,
                    config_schema_json=None,
                )
            )
            await session.commit()
            return None
        return await real_read(session, did)

    with (
        patch.object(driver_loader, "_read_cache_row", new=_read),
        patch.object(
            driver_loader,
            "download_driver_package",
            new=AsyncMock(return_value=_make_zip(VALID_L1_DRIVER)),
        ),
        patch.object(driver_loader, "extract_config_schema_json", return_value=None),
        patch("app.services.driver_loader.settings") as mock_settings,
    ):
        _patch_loader_settings(mock_settings, str(cache_root))
        path = await load_driver(db, driver_id, "sha-1", "driver.zip", "Layer 1 Switch")

    assert path == str(winner_dir)
    assert list(cache_root.iterdir()) == []
    rows = (
        (await db.execute(select(DriverCache).where(DriverCache.driver_id == driver_id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].local_path == str(winner_dir)


@pytest.mark.asyncio
async def test_load_driver_replaces_row_of_another_package_and_removes_its_directory(db, tmp_path):
    """A row for a different sha256 (or a missing directory) is updated in place to
    this load's directory, and the replaced directory is removed."""
    from app.services import driver_loader

    driver_id = uuid.uuid4()
    old_dir = tmp_path / f"{driver_id}-old"
    old_dir.mkdir()
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    db.add(
        DriverCache(
            driver_id=driver_id,
            sha256="old-sha",
            local_path=str(old_dir),
            metadata_json=None,
            config_schema_json=None,
        )
    )
    await db.commit()

    with (
        patch.object(driver_loader, "get_cached_driver", new=AsyncMock(return_value=None)),
        patch.object(
            driver_loader,
            "download_driver_package",
            new=AsyncMock(return_value=_make_zip(VALID_L1_DRIVER)),
        ),
        patch.object(driver_loader, "extract_config_schema_json", return_value=None),
        patch("app.services.driver_loader.settings") as mock_settings,
    ):
        _patch_loader_settings(mock_settings, str(cache_root))
        path = await load_driver(db, driver_id, "new-sha", "driver.zip", "Layer 1 Switch")

    assert Path(path).parent == cache_root
    assert Path(path).is_dir()
    assert not old_dir.exists()
    row = (
        await db.execute(select(DriverCache).where(DriverCache.driver_id == driver_id))
    ).scalar_one()
    assert row.sha256 == "new-sha"
    assert row.local_path == path
