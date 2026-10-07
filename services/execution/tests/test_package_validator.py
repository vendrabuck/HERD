"""Tests for the recipe-package validator and its internal endpoint (issue #28 phase 1).

Covers the AST-based structural section (the validator never imports the
package in-process), the stricter generated-recipe policy contract
(supports_dry_run required, stdlib-only, no inline credentials, no _deps or
requirements.txt), the sandboxed dry-run lifecycle including instance_ref
threading, the keyed destroy step (issue #937), and driver-level failure
verdicts, decode errors and the size cap,
temp-dir cleanup, the real drivers/mock_hypervisor package end to end, and
the internal route (auth wording, unsupported connection type, report shape).
"""

import base64
import io
import json
import tempfile
import zipfile
from pathlib import Path

import pytest
from app.config import settings
from app.main import app
from app.services.package_validator import (
    KEYED_DESTROY_STEP,
    PackageDecodeError,
    validate_package,
)
from httpx import ASGITransport, AsyncClient

GOOD_METADATA = {
    "name": "test-recipe",
    "version": "1.0.0",
    "connection_type": "Hypervisor",
    "supports_dry_run": True,
}

GOOD_DRIVER = '''
"""A well-behaved generated recipe used as the validator's known-good input."""


class Driver:
    def __init__(self, context):
        self.context = context
        self.dry_run = bool(context.get("dry_run", False))

    @classmethod
    def config_schema(cls):
        return {
            "type": "object",
            "properties": {"note": {"type": "string"}},
            "additionalProperties": False,
        }

    def login(self):
        return {"success": True, "simulated": self.dry_run}

    def logout(self):
        return {"success": True}

    def create_instance(self, **_):
        ref = "sim-" + str(self.context.get("HERD_request_id", ""))[:8]
        return {
            "success": True,
            "instance_ref": ref,
            "field_data": {"management_ip": "192.0.2.10"},
        }

    def status(self):
        return {"success": True, "state": "simulated"}

    def destroy_instance(self, instance_ref=None, **_):
        return {"success": True, "received_ref": instance_ref}
'''


def make_package_b64(files: dict[str, str]) -> str:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return base64.b64encode(buf.getvalue()).decode()


def good_package_b64(**overrides) -> str:
    files = {
        "driver.py": GOOD_DRIVER,
        "driver_metadata.json": json.dumps(GOOD_METADATA),
    }
    files.update(overrides)
    return make_package_b64(files)


def run(package_b64: str, connection_type: str = "Hypervisor") -> dict:
    return validate_package(package_b64, "package.zip", connection_type)


# --- happy path ---


def test_good_package_is_valid_across_all_sections():
    report = run(good_package_b64())
    assert report["structural"] == {"passed": True, "errors": []}
    assert report["policy"] == {"passed": True, "errors": []}
    assert report["schema"]["present"] is True
    assert report["schema"]["schema"]["additionalProperties"] is False
    assert report["dry_run"]["passed"] is True
    assert report["valid"] is True

    actions = [m["action"] for m in report["dry_run"]["methods"]]
    assert actions == [
        "login",
        "create_instance",
        "status",
        "destroy_instance",
        "destroy_instance (no instance_ref)",
        "logout",
    ]
    assert KEYED_DESTROY_STEP == "destroy_instance (no instance_ref)"
    assert all(m["passed"] for m in report["dry_run"]["methods"])


def test_dry_run_threads_instance_ref_from_create_to_destroy():
    report = run(good_package_b64())
    by_action = {m["action"]: m for m in report["dry_run"]["methods"]}
    ref = by_action["create_instance"]["output"]["instance_ref"]
    assert ref.startswith("sim-")
    assert by_action["destroy_instance"]["output"]["received_ref"] == ref
    # The keyed step passes instance_ref=None explicitly, the consumer's shape.
    assert by_action[KEYED_DESTROY_STEP]["output"]["received_ref"] is None


def test_schema_is_optional_and_does_not_gate_validity():
    driver_no_schema = GOOD_DRIVER.replace("@classmethod", "@staticmethod").replace(
        "def config_schema(cls):", "def _not_a_schema():"
    )
    report = run(good_package_b64(**{"driver.py": driver_no_schema}))
    assert report["schema"]["present"] is False
    assert report["valid"] is True


def test_repo_mock_hypervisor_package_validates():
    mock_dir = Path(__file__).parents[3] / "drivers" / "mock_hypervisor"
    files = {p.name: p.read_text() for p in mock_dir.iterdir() if p.is_file()}
    report = run(make_package_b64(files))
    assert report["structural"]["errors"] == []
    assert report["policy"]["errors"] == []
    assert report["valid"] is True, report["dry_run"]
    # The mock's keyed destroy names the same instance its create named.
    by_action = {m["action"]: m for m in report["dry_run"]["methods"]}
    keyed = by_action[KEYED_DESTROY_STEP]["output"]
    assert keyed["keyed"] is True
    assert keyed["instance_ref"] == by_action["create_instance"]["output"]["instance_ref"]


# --- structural section (AST only, never imported) ---


def test_missing_driver_py_fails_structural():
    report = run(make_package_b64({"driver_metadata.json": json.dumps(GOOD_METADATA)}))
    assert report["valid"] is False
    assert report["structural"]["errors"] == ["Missing driver.py at package root"]
    assert report["dry_run"]["error"] == "not run"


def test_missing_required_method_fails_structural():
    broken = GOOD_DRIVER.replace("def destroy_instance", "def destroy_instance_typo")
    report = run(good_package_b64(**{"driver.py": broken}))
    assert report["valid"] is False
    assert report["structural"]["errors"] == [
        "Driver class is missing required method: destroy_instance"
    ]


def test_unparseable_driver_py_fails_structural():
    report = run(good_package_b64(**{"driver.py": "class Driver(:\n    pass"}))
    assert report["valid"] is False
    assert len(report["structural"]["errors"]) == 1
    assert report["structural"]["errors"][0].startswith("driver.py failed to parse")


def test_missing_driver_class_fails_structural():
    report = run(good_package_b64(**{"driver.py": "class NotADriver:\n    pass"}))
    assert report["structural"]["errors"] == [
        "driver.py must define a top-level class named Driver"
    ]


def test_structural_failure_never_executes_the_package(tmp_path):
    # A package whose import would create a sentinel file. The validator is
    # AST-only and must skip the sandbox for a structurally broken package,
    # so the sentinel must not exist afterward.
    sentinel = tmp_path / "executed.flag"
    trojan = f'open(r"{sentinel}", "w").write("ran")\nclass NotADriver:\n    pass\n'
    report = run(good_package_b64(**{"driver.py": trojan}))
    assert report["valid"] is False
    assert not sentinel.exists()


# --- policy section (the stricter generated-recipe contract) ---


def test_missing_metadata_fails_policy():
    report = run(make_package_b64({"driver.py": GOOD_DRIVER}))
    assert "driver_metadata.json is required for generated recipes" in report["policy"]["errors"]
    assert report["valid"] is False


def test_supports_dry_run_false_fails_policy():
    meta = dict(GOOD_METADATA, supports_dry_run=False)
    report = run(good_package_b64(**{"driver_metadata.json": json.dumps(meta)}))
    assert any("supports_dry_run: true" in e for e in report["policy"]["errors"])
    assert report["valid"] is False


def test_non_stdlib_import_fails_policy():
    driver = "import requests\n" + GOOD_DRIVER
    report = run(good_package_b64(**{"driver.py": driver}))
    assert any(
        "'requests' is neither standard library nor package-local" in e
        for e in report["policy"]["errors"]
    )
    assert report["valid"] is False


def test_package_local_import_is_allowed():
    driver = "import helpers\n" + GOOD_DRIVER
    report = run(good_package_b64(**{"driver.py": driver, "helpers.py": "VALUE = 1\n"}))
    assert report["policy"]["errors"] == []
    assert report["valid"] is True


def test_deps_dir_and_requirements_fail_policy():
    report = run(
        good_package_b64(**{"_deps/vendored.py": "x = 1\n", "requirements.txt": "requests\n"})
    )
    errors = report["policy"]["errors"]
    assert any("_deps/ vendoring is not allowed" in e for e in errors)
    assert any("requirements.txt is not allowed" in e for e in errors)


@pytest.mark.parametrize(
    "snippet",
    [
        'password = "hunter2"',
        'self.api_token = "abc123"',
        'creds = {"password": "hunter2"}',
        'connect(secret="abc123")',
    ],
)
def test_inline_credential_literal_fails_policy(snippet):
    driver = GOOD_DRIVER + f"\n\ndef _helper():\n    {snippet}\n"
    report = run(good_package_b64(**{"driver.py": driver}))
    assert any("credential-like name" in e for e in report["policy"]["errors"]), (
        snippet,
        report["policy"]["errors"],
    )


@pytest.mark.parametrize(
    "snippet",
    [
        'password = ""',
        'password = context.get("HERD_password")',
        'token = "Bearer " + value',
    ],
)
def test_non_literal_or_empty_credentials_are_allowed(snippet):
    driver = GOOD_DRIVER + f"\n\ndef _helper(context, value):\n    {snippet}\n"
    report = run(good_package_b64(**{"driver.py": driver}))
    assert report["policy"]["errors"] == [], report["policy"]["errors"]


# --- dry-run section ---


def test_method_exception_fails_dry_run():
    broken = GOOD_DRIVER.replace(
        'return {"success": True, "state": "simulated"}',
        'raise RuntimeError("boom")',
    )
    report = run(good_package_b64(**{"driver.py": broken}))
    assert report["structural"]["passed"] is True
    assert report["policy"]["passed"] is True
    assert report["dry_run"]["passed"] is False
    assert report["valid"] is False
    by_action = {m["action"]: m for m in report["dry_run"]["methods"]}
    assert by_action["status"]["passed"] is False
    assert by_action["login"]["passed"] is True
    # The report keeps the exception MESSAGE, not only its class: it is the
    # repair signal the recipe-authoring loop feeds back to the model, and
    # validation runs against a synthetic context with nothing real to leak.
    # The execute path deliberately stores the class name alone (issue #840).
    assert by_action["status"]["error"] == "RuntimeError: boom"


def test_driver_level_failure_verdict_fails_dry_run():
    # Sandbox-level success with an explicit driver-level failure verdict:
    # run-row status is sandbox-level, the driver verdict lives in the output
    # JSON, and validation requires both to agree.
    broken = GOOD_DRIVER.replace(
        'return {"success": True, "state": "simulated"}',
        'return {"success": False, "error": "simulated failure"}',
    )
    report = run(good_package_b64(**{"driver.py": broken}))
    by_action = {m["action"]: m for m in report["dry_run"]["methods"]}
    assert by_action["status"]["success"] is True
    assert by_action["status"]["passed"] is False
    assert report["valid"] is False


# A recipe written to the pre-#937 contract: destroy assumes it always gets a
# ref. The by-ref step passes; the keyed step must fail validation.
_LEGACY_DESTROY_RAISES = """
    def destroy_instance(self, instance_ref=None, **_):
        return {"success": True, "received_ref": instance_ref.upper()}
"""
_LEGACY_DESTROY_REQUIRED_ARG = """
    def destroy_instance(self, instance_ref):
        if instance_ref is None:
            return {"success": False, "error": "instance_ref is required"}
        return {"success": True, "received_ref": instance_ref}
"""


def _with_destroy(body: str) -> str:
    original = """
    def destroy_instance(self, instance_ref=None, **_):
        return {"success": True, "received_ref": instance_ref}
"""
    assert original in GOOD_DRIVER
    return GOOD_DRIVER.replace(original, body)


@pytest.mark.parametrize(
    "destroy_body, expected_error",
    [
        (_LEGACY_DESTROY_RAISES, "AttributeError: 'NoneType' object has no attribute 'upper'"),
        (
            _LEGACY_DESTROY_REQUIRED_ARG,
            'destroy_instance must return "success": true; the execution consumer '
            "treats a missing or false success as a failed destroy",
        ),
    ],
)
def test_recipe_that_cannot_destroy_without_instance_ref_fails_validation(
    destroy_body, expected_error
):
    report = run(good_package_b64(**{"driver.py": _with_destroy(destroy_body)}))
    by_action = {m["action"]: m for m in report["dry_run"]["methods"]}
    assert by_action["destroy_instance"]["passed"] is True
    assert by_action[KEYED_DESTROY_STEP]["passed"] is False
    assert by_action[KEYED_DESTROY_STEP]["error"] == expected_error
    assert report["dry_run"]["passed"] is False
    assert report["valid"] is False


# --- the validator judges by the consumer's rule (issue #937 review) ---


def test_validator_shares_the_consumer_predicates():
    """One rule, not a copy: the validator and the consumer import the same
    functions, so they cannot drift."""
    from app.services import nats_consumer, package_validator

    assert package_validator.recipe_reported_success is nats_consumer._recipe_reported_success
    assert package_validator.created_instance_ref is nats_consumer._created_instance_ref
    assert package_validator.recipe_session_succeeded is nats_consumer._recipe_session_succeeded


_CREATE_RULE_TEXT = (
    'create_instance must return "success": true and a non-empty string "instance_ref"; '
    "the execution consumer treats anything else as a failed create"
)


@pytest.mark.parametrize(
    "create_return",
    [
        # No success key: _method_passed used to call this a pass.
        '{"instance_ref": "sim-1", "field_data": {}}',
        # Success without a usable handle.
        '{"success": True, "field_data": {}}',
        '{"success": True, "instance_ref": "", "field_data": {}}',
        '{"success": True, "instance_ref": 4711, "field_data": {}}',
    ],
)
def test_create_that_the_consumer_would_reject_fails_validation(create_return):
    original = """        return {
            "success": True,
            "instance_ref": ref,
            "field_data": {"management_ip": "192.0.2.10"},
        }"""
    assert original in GOOD_DRIVER
    driver = GOOD_DRIVER.replace(original, f"        return {create_return}")
    report = run(good_package_b64(**{"driver.py": driver}))
    by_action = {m["action"]: m for m in report["dry_run"]["methods"]}
    assert by_action["create_instance"]["passed"] is False
    assert by_action["create_instance"]["error"] == _CREATE_RULE_TEXT
    assert report["valid"] is False


def test_destroy_without_a_success_key_fails_validation():
    report = run(
        good_package_b64(
            **{
                "driver.py": _with_destroy(
                    """
    def destroy_instance(self, instance_ref=None, **_):
        return {"received_ref": instance_ref}
"""
                )
            }
        )
    )
    by_action = {m["action"]: m for m in report["dry_run"]["methods"]}
    assert by_action["destroy_instance"]["passed"] is False
    assert by_action[KEYED_DESTROY_STEP]["passed"] is False
    assert report["valid"] is False


@pytest.mark.parametrize(
    "login_return, passed",
    [
        # A present, falsy success is a failed login for the consumer (issue
        # #1027); _method_passed only refused a literal False.
        ('{"success": 0}', False),
        ('{"success": None}', False),
        ('{"success": False, "error": "auth denied"}', False),
        # A missing key stays a pass on both sides, so a recipe the validator
        # approves is never refused by the consumer on a missing key.
        ('{"session": "abc"}', True),
    ],
)
def test_login_is_judged_by_the_consumer_session_rule(login_return, passed):
    original = """    def login(self):
        return {"success": True, "simulated": self.dry_run}"""
    assert original in GOOD_DRIVER
    driver = GOOD_DRIVER.replace(original, f"    def login(self):\n        return {login_return}")
    report = run(good_package_b64(**{"driver.py": driver}))
    by_action = {m["action"]: m for m in report["dry_run"]["methods"]}
    assert by_action["login"]["passed"] is passed
    assert report["valid"] is passed


def test_keyed_destroy_step_runs_with_request_id_in_context():
    """The keyed step's only handle is HERD_request_id; a recipe that derives
    its name from it (as create_instance must) passes."""
    body = """
    def destroy_instance(self, instance_ref=None, **_):
        if instance_ref is None:
            request_id = self.context["HERD_request_id"]
            return {"success": True, "keyed_name": "sim-" + request_id[:8]}
        return {"success": True, "received_ref": instance_ref}
"""
    report = run(good_package_b64(**{"driver.py": _with_destroy(body)}))
    by_action = {m["action"]: m for m in report["dry_run"]["methods"]}
    assert report["valid"] is True
    assert (
        by_action[KEYED_DESTROY_STEP]["output"]["keyed_name"]
        == by_action["create_instance"]["output"]["instance_ref"]
    )


def test_import_time_failure_surfaces_in_sandbox_not_in_process():
    # Top-level code that raises at import: structurally fine (AST parses,
    # class exists), so it reaches the sandbox, where every method run fails
    # in the subprocess. This process never imports the package.
    driver = 'raise RuntimeError("import bomb")\n' + GOOD_DRIVER
    report = run(good_package_b64(**{"driver.py": driver}))
    assert report["structural"]["passed"] is True
    assert report["dry_run"]["passed"] is False
    assert report["valid"] is False


# --- decode errors, size cap, cleanup ---


def test_invalid_base64_raises_decode_error():
    with pytest.raises(PackageDecodeError, match="package_b64 is not valid base64"):
        run("not base64!!")


def test_size_cap_raises_decode_error(monkeypatch):
    monkeypatch.setattr(settings, "validate_package_max_bytes", 10)
    with pytest.raises(PackageDecodeError, match="exceeds the 10 byte validation limit"):
        run(good_package_b64())


def test_corrupt_archive_reports_extraction_failure():
    garbage = base64.b64encode(b"this is not a zip archive").decode()
    report = run(garbage)
    assert report["valid"] is False
    assert report["structural"]["errors"][0].startswith("Failed to extract package")


def test_no_temp_dirs_left_behind():
    tmp_root = Path(tempfile.gettempdir())
    before = set(tmp_root.glob("herd_validate_*"))
    run(good_package_b64())
    run(make_package_b64({"driver.py": "class Driver(:\n"}))
    after = set(tmp_root.glob("herd_validate_*"))
    assert after == before


# --- the internal route ---


@pytest.fixture
async def client(monkeypatch):
    monkeypatch.setattr(settings, "internal_api_token", "test-internal-token")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


HEADERS = {"X-Internal-Token": "test-internal-token"}


@pytest.mark.asyncio
async def test_route_requires_internal_token(client):
    resp = await client.post(
        "/internal/validate-package",
        json={"package_b64": good_package_b64()},
        headers={"X-Internal-Token": "wrong"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Invalid internal token"


@pytest.mark.asyncio
async def test_route_500_when_token_unconfigured(client, monkeypatch):
    monkeypatch.setattr(settings, "internal_api_token", "")
    resp = await client.post(
        "/internal/validate-package",
        json={"package_b64": good_package_b64()},
        headers=HEADERS,
    )
    assert resp.status_code == 500
    assert resp.json()["detail"] == "Internal API token not configured"


@pytest.mark.asyncio
async def test_route_rejects_unsupported_connection_type(client):
    resp = await client.post(
        "/internal/validate-package",
        json={"package_b64": good_package_b64(), "connection_type": "Layer 1 Switch"},
        headers=HEADERS,
    )
    assert resp.status_code == 422
    assert (
        resp.json()["detail"]
        == "Only the Hypervisor connection type is supported for package validation"
    )


@pytest.mark.asyncio
async def test_route_rejects_invalid_base64(client):
    resp = await client.post(
        "/internal/validate-package",
        json={"package_b64": "not base64!!"},
        headers=HEADERS,
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "package_b64 is not valid base64"


@pytest.mark.asyncio
async def test_route_happy_path_report_shape(client):
    resp = await client.post(
        "/internal/validate-package",
        json={"package_b64": good_package_b64()},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is True
    assert data["structural"] == {"passed": True, "errors": []}
    assert data["policy"] == {"passed": True, "errors": []}
    # The schema section serializes under the wire name "schema".
    assert data["schema"]["present"] is True
    assert [m["action"] for m in data["dry_run"]["methods"]] == [
        "login",
        "create_instance",
        "status",
        "destroy_instance",
        "destroy_instance (no instance_ref)",
        "logout",
    ]


@pytest.mark.asyncio
async def test_route_returns_red_report_not_error_for_broken_package(client):
    broken = GOOD_DRIVER.replace("def create_instance", "def create_instance_typo")
    resp = await client.post(
        "/internal/validate-package",
        json={"package_b64": make_package_b64({"driver.py": broken})},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is False
    assert (
        "Driver class is missing required method: create_instance" in (data["structural"]["errors"])
    )
