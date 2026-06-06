import io
import tempfile
import zipfile
from pathlib import Path

import pytest
from app.services.driver_loader import extract_driver_package, validate_driver


def _make_driver_zip(driver_code: str, extra_files: dict | None = None) -> bytes:
    """Create a .zip archive in memory with driver.py and optional extra files."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("driver.py", driver_code)
        if extra_files:
            for name, content in extra_files.items():
                zf.writestr(name, content)
    return buf.getvalue()


VALID_L1_DRIVER = """
class Driver:
    def __init__(self, context):
        self.context = context

    def login(self):
        return {"success": True}

    def logout(self):
        return {"success": True}

    def connect_ports(self, port_a, port_b):
        return {"success": True}

    def disconnect_ports(self, port_a, port_b):
        return {"success": True}

    def status(self):
        return {"reachable": True}
"""

MISSING_METHOD_DRIVER = """
class Driver:
    def __init__(self, context):
        self.context = context

    def login(self):
        return {"success": True}

    def logout(self):
        return {"success": True}
    # Missing connect_ports, disconnect_ports, status
"""


@pytest.mark.asyncio
async def test_extract_zip():
    """Extract a valid zip archive."""
    zip_bytes = _make_driver_zip(VALID_L1_DRIVER)
    with tempfile.TemporaryDirectory() as tmpdir:
        dest = Path(tmpdir) / "driver"
        extract_driver_package(zip_bytes, "test.zip", dest)
        assert (dest / "driver.py").exists()


@pytest.mark.asyncio
async def test_extract_unsupported_format():
    """Unsupported format raises ValueError."""
    with tempfile.TemporaryDirectory() as tmpdir:
        dest = Path(tmpdir) / "driver"
        with pytest.raises(ValueError, match="Unsupported"):
            extract_driver_package(b"data", "test.exe", dest)


@pytest.mark.asyncio
async def test_validate_valid_l1_driver():
    """Valid L1 driver passes validation."""
    zip_bytes = _make_driver_zip(VALID_L1_DRIVER)
    with tempfile.TemporaryDirectory() as tmpdir:
        dest = Path(tmpdir) / "driver"
        extract_driver_package(zip_bytes, "test.zip", dest)
        errors = validate_driver(dest, "Layer 1 Switch")
        assert errors == []


@pytest.mark.asyncio
async def test_validate_missing_driver_py():
    """Missing driver.py fails validation."""
    with tempfile.TemporaryDirectory() as tmpdir:
        dest = Path(tmpdir) / "driver"
        dest.mkdir()
        errors = validate_driver(dest, "Layer 1 Switch")
        assert any("Missing driver.py" in e for e in errors)


@pytest.mark.asyncio
async def test_validate_missing_driver_class():
    """driver.py without Driver class fails validation."""
    code = "def helper(): pass"
    zip_bytes = _make_driver_zip(code)
    with tempfile.TemporaryDirectory() as tmpdir:
        dest = Path(tmpdir) / "driver"
        extract_driver_package(zip_bytes, "test.zip", dest)
        errors = validate_driver(dest, "Layer 1 Switch")
        assert any("class named Driver" in e for e in errors)


@pytest.mark.asyncio
async def test_validate_missing_methods():
    """Driver with missing methods fails validation."""
    zip_bytes = _make_driver_zip(MISSING_METHOD_DRIVER)
    with tempfile.TemporaryDirectory() as tmpdir:
        dest = Path(tmpdir) / "driver"
        extract_driver_package(zip_bytes, "test.zip", dest)
        errors = validate_driver(dest, "Layer 1 Switch")
        assert len(errors) == 3  # connect_ports, disconnect_ports, status
        assert any("connect_ports" in e for e in errors)
        assert any("disconnect_ports" in e for e in errors)
        assert any("status" in e for e in errors)


@pytest.mark.asyncio
async def test_validate_unknown_connection_type():
    """Unknown connection type fails validation."""
    zip_bytes = _make_driver_zip(VALID_L1_DRIVER)
    with tempfile.TemporaryDirectory() as tmpdir:
        dest = Path(tmpdir) / "driver"
        extract_driver_package(zip_bytes, "test.zip", dest)
        errors = validate_driver(dest, "Unknown Type")
        assert any("Unknown connection type" in e for e in errors)


@pytest.mark.asyncio
async def test_validate_syntax_error_driver():
    """driver.py with syntax error fails validation."""
    code = "class Driver:\n    def __init__(self, context\n"  # syntax error
    zip_bytes = _make_driver_zip(code)
    with tempfile.TemporaryDirectory() as tmpdir:
        dest = Path(tmpdir) / "driver"
        extract_driver_package(zip_bytes, "test.zip", dest)
        errors = validate_driver(dest, "Layer 1 Switch")
        assert any("Failed to load" in e for e in errors)


@pytest.mark.asyncio
async def test_extract_with_extra_files():
    """Extra files like lib/ are extracted."""
    extras = {"lib/utils.py": "HELPER = True", "requirements.txt": "requests"}
    zip_bytes = _make_driver_zip(VALID_L1_DRIVER, extras)
    with tempfile.TemporaryDirectory() as tmpdir:
        dest = Path(tmpdir) / "driver"
        extract_driver_package(zip_bytes, "test.zip", dest)
        assert (dest / "driver.py").exists()
        assert (dest / "lib" / "utils.py").exists()
        assert (dest / "requirements.txt").exists()
