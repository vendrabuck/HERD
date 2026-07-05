"""Unit tests for the checked-in mock Hypervisor recipe driver (issue #32).

Pure, stack-free: load drivers/mock_hypervisor/driver.py by path and exercise
the Driver class directly. Guards the recipe contract the execution consumer
depends on (the Hypervisor method set, the create_instance
{success, instance_ref, field_data} shape its instance-ref extraction reads,
the HERD_request_id determinism the redelivery-idempotency story relies on,
destroy idempotency, and the failure-injection knobs the dynamic-resources
integration tests use) without needing Docker.
"""

import importlib.util
import json
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DRIVER_DIR = _REPO_ROOT / "drivers" / "mock_hypervisor"
_DRIVER_PATH = _DRIVER_DIR / "driver.py"

# The execution loader (driver_loader.validate_driver) requires exactly these
# methods for a "Hypervisor" package; pin them here so a rename breaks in this
# fast unit test rather than only in a stack-only integration run.
HYPERVISOR_REQUIRED_METHODS = (
    "login",
    "logout",
    "create_instance",
    "destroy_instance",
    "status",
)

REQUEST_ID = "7f3a1a52-9d3e-4a6f-8c1b-2e4d5f6a7b8c"


def _context(**overrides):
    """A minimal recipe context, mirroring nats_consumer._build_recipe_context."""
    ctx = {
        "HERD_request_id": REQUEST_ID,
        "HERD_reservation_id": "res-1",
        "HERD_user_id": "user-1",
        "HERD_hypervisor_endpoint": "https://mock-hv.example:8006",
        "HERD_hypervisor_type": "mock",
        "HERD_secret_username": "svc",
        "HERD_secret_password": "password",
    }
    ctx.update(overrides)
    return ctx


@pytest.fixture(scope="module")
def driver_cls():
    spec = importlib.util.spec_from_file_location("mock_hypervisor_driver", _DRIVER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Driver


def test_exposes_the_hypervisor_method_set(driver_cls):
    d = driver_cls({})
    for name in HYPERVISOR_REQUIRED_METHODS:
        assert callable(getattr(d, name)), f"missing required Hypervisor method {name}"


def test_metadata_declares_hypervisor_and_dry_run():
    meta = json.loads((_DRIVER_DIR / "driver_metadata.json").read_text())
    assert meta["connection_type"] == "Hypervisor"
    assert meta["supports_dry_run"] is True


def test_login_logout_shapes(driver_cls):
    d = driver_cls(_context())
    assert d.login() == {"success": True, "output": {}}
    assert d.logout() == {"success": True, "output": {}}


def test_create_instance_success_shape(driver_cls):
    """The exact shape the consumer reads: top-level success, instance_ref, and
    field_data (nats_consumer._recipe_reported_success and the extraction at
    output.get("instance_ref") / output.get("field_data"))."""
    res = driver_cls(_context()).create_instance()
    assert res["success"] is True
    assert isinstance(res["instance_ref"], str) and res["instance_ref"]
    assert REQUEST_ID in res["instance_ref"]
    assert isinstance(res["field_data"], dict)
    assert res["field_data"]["mgmt_address"].startswith("10.66.")
    # Nothing is nested under an "output" key; the contract keys are top-level.
    assert "output" not in res


def test_create_instance_is_deterministic_per_request_id(driver_cls):
    """Same HERD_request_id, same instance_ref and attributes: the redelivery
    idempotency contract for recipe authors."""
    first = driver_cls(_context()).create_instance()
    second = driver_cls(_context()).create_instance()
    assert first == second

    other = driver_cls(_context(HERD_request_id="another-request")).create_instance()
    assert other["instance_ref"] != first["instance_ref"]


def test_create_instance_echoes_template_image_field(driver_cls):
    res = driver_cls(_context(HERD_image="ubuntu-22.04")).create_instance()
    assert res["field_data"]["image"] == "ubuntu-22.04"


def test_create_instance_without_request_id_reports_failure(driver_cls):
    res = driver_cls(_context(HERD_request_id=None)).create_instance()
    assert res["success"] is False
    assert "HERD_request_id" in res["error"]


def test_destroy_instance_succeeds_and_is_idempotent(driver_cls):
    """Destroy acknowledges with success, including for an absent instance
    (this mock is stateless), per the ADR 0004 idempotency requirement."""
    d = driver_cls(_context())
    first = d.destroy_instance(instance_ref=f"mock-vm-{REQUEST_ID}")
    assert first == {"success": True, "instance_ref": f"mock-vm-{REQUEST_ID}"}
    # A repeat destroy (the instance is already gone) still succeeds.
    assert d.destroy_instance(instance_ref=f"mock-vm-{REQUEST_ID}")["success"] is True
    # Never-created / unknown refs also succeed.
    assert d.destroy_instance(instance_ref="mock-vm-never-created")["success"] is True


def test_fail_injection_returns_unsuccessful_result(driver_cls):
    d = driver_cls(_context(HERD_mock_fail_actions="create_instance,destroy_instance"))
    create = d.create_instance()
    assert create["success"] is False
    assert "injected" in create["error"]
    assert "instance_ref" not in create
    destroy = d.destroy_instance(instance_ref="mock-vm-x")
    assert destroy["success"] is False
    # Actions not in the injected set still succeed.
    assert d.login()["success"] is True


def test_raise_injection_raises(driver_cls):
    d = driver_cls(_context(HERD_mock_raise_actions="create_instance"))
    with pytest.raises(RuntimeError, match="injected raise on create_instance"):
        d.create_instance()
    d2 = driver_cls(_context(HERD_mock_raise_actions="login"))
    with pytest.raises(RuntimeError, match="injected raise on login"):
        d2.login()


def test_sleep_injection_delays_each_call(driver_cls):
    d = driver_cls(_context(HERD_mock_sleep_ms="120"))
    started = time.monotonic()
    assert d.create_instance()["success"] is True
    assert time.monotonic() - started >= 0.12


def test_dry_run_flags_simulated(driver_cls):
    d = driver_cls(_context(dry_run=True))
    res = d.create_instance()
    assert res["success"] is True
    assert res["simulated"] is True
    assert res["instance_ref"] == f"mock-vm-{REQUEST_ID}"
    assert d.destroy_instance(instance_ref=res["instance_ref"])["simulated"] is True


def test_status_reports_reachable_and_never_raises(driver_cls):
    assert driver_cls(_context()).status() == {"reachable": True, "simulated": False}
    # Even with failure injection, status answers rather than raising.
    fail = driver_cls(_context(HERD_mock_fail_actions="status")).status()
    assert fail["reachable"] is False
    # Raise injection is ignored on status; it must always answer.
    assert driver_cls(_context(HERD_mock_raise_actions="status")).status()["reachable"] is True
