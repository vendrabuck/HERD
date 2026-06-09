"""Tests for driver-published config-schema extraction (issue #23, slice 1).

Covers the __config_schema__ sentinel action in _runner.py and the
extract_config_schema sandbox wrapper. The contract:

- a valid config_schema() classmethod yields {"has_schema": True, "schema": ...}
- a driver with no config_schema() yields {"has_schema": False, "schema": None}
- a config_schema() that raises or returns a non-dict must NOT crash the run;
  callers treat it as no usable schema and fall back to the registry
- the classmethod is invoked WITHOUT instantiating Driver(context), so a driver
  whose __init__ needs live credentials still extracts cleanly
- a runaway config_schema() is killed by the rlimit/timeout and surfaces as a
  failed run, never a hang or an exception

See docs/design/0002-driver-published-config-schemas.md.
"""

import tempfile
from pathlib import Path

import pytest
from app.services.driver_sandbox import extract_config_schema

FIXTURE_DRIVERS = Path(__file__).resolve().parent / "fixtures" / "drivers"


def _temp_driver(driver_code: str) -> tempfile.TemporaryDirectory:
    tmpdir = tempfile.TemporaryDirectory()
    (Path(tmpdir.name) / "driver.py").write_text(driver_code)
    return tmpdir


# --- (a) valid published schema ----------------------------------------------


def test_extract_valid_schema_from_fixture():
    result = extract_config_schema(str(FIXTURE_DRIVERS / "mock_schema_mgmt"))
    assert result["success"] is True
    output = result["output"]
    assert output["has_schema"] is True
    schema = output["schema"]
    assert schema["type"] == "object"
    assert "vlan" in schema["properties"]
    assert "hostname" in schema["properties"]
    # The published schema is narrower than the Management registry entry: it
    # omits `ip`. This is what lets it override the registry downstream.
    assert "ip" not in schema["properties"]
    assert schema["additionalProperties"] is False


# --- (b) no config_schema() method -------------------------------------------


def test_extract_no_schema_from_fixture():
    result = extract_config_schema(str(FIXTURE_DRIVERS / "mock_no_schema"))
    assert result["success"] is True
    assert result["output"] == {"has_schema": False, "schema": None}


# --- (c) non-dict / raises is fail-open --------------------------------------


def test_extract_schema_returning_non_dict():
    """A non-dict return still serializes; the caller, not the runner, decides
    it is unusable and falls back to the registry."""
    code = """
class Driver:
    def __init__(self, context):
        self.context = context
    @classmethod
    def config_schema(cls):
        return "not-a-dict"
"""
    tmpdir = _temp_driver(code)
    with tmpdir:
        result = extract_config_schema(tmpdir.name)
    assert result["success"] is True
    # has_schema is True (the method exists and returned), but the schema is not
    # a dict; the inventory/cache layer rejects non-dicts and falls back.
    assert result["output"]["has_schema"] is True
    assert not isinstance(result["output"]["schema"], dict)


def test_extract_schema_that_raises_is_failed_run():
    """A config_schema() that raises surfaces as a failed run (success=False),
    never an unhandled crash; the caller falls back to the registry."""
    code = """
class Driver:
    def __init__(self, context):
        self.context = context
    @classmethod
    def config_schema(cls):
        raise RuntimeError("schema build blew up")
"""
    tmpdir = _temp_driver(code)
    with tmpdir:
        result = extract_config_schema(tmpdir.name)
    assert result["success"] is False
    assert result["output"] is None
    assert "schema build blew up" in (result["error"] or "")


# --- (d) classmethod invoked WITHOUT instantiation ---------------------------


def test_extract_does_not_instantiate_credential_dependent_init():
    """The credential-dependent __init__ reads context["HERD_ip_address"] and
    raises KeyError on the empty context the extraction path passes. If the
    runner instantiated the driver, this would fail; it must not."""
    result = extract_config_schema(str(FIXTURE_DRIVERS / "mock_credential_init"))
    assert result["success"] is True
    assert result["output"]["has_schema"] is True
    assert result["output"]["schema"]["properties"]["hostname"]["maxLength"] == 64


# --- (e) timeout / runaway is no-schema, not a crash -------------------------


@pytest.mark.skipif(
    not hasattr(__import__("os"), "fork"),
    reason="rlimit/preexec_fn only applies on POSIX",
)
def test_extract_schema_timeout_surfaces_as_failed_run():
    """A config_schema() that sleeps past the timeout is killed; the wrapper
    returns a failed run with a timeout error rather than hanging."""
    code = """
import time
class Driver:
    def __init__(self, context):
        self.context = context
    @classmethod
    def config_schema(cls):
        time.sleep(30)
        return {}
"""
    tmpdir = _temp_driver(code)
    with tmpdir:
        result = extract_config_schema(tmpdir.name, timeout=1)
    assert result["success"] is False
    assert result["output"] is None
    assert "timed out" in (result["error"] or "")
