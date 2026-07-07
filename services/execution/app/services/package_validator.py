"""Validator for unapproved recipe packages (ADR 0005, issue #28, phase 1).

Validates a driver package WITHOUT executing it in this process. The load
path's validate_driver imports the module in-process, which is fine for a
package an admin already approved and uploaded, but this validator exists
for AI-drafted packages that nobody has approved yet, so:

- structural checks are AST-based (never imported, never executed here);
- everything that must run the code (config-schema extraction, the dry-run
  lifecycle) runs in the rlimit sandbox subprocess, and only after the
  static sections passed.

The report is additive: every section reports independently so the
drafting loop upstream (ai-orchestrator) can feed precise failures back to
the model, and an admin reviewing a draft sees exactly what was checked.
"""

import ast
import base64
import binascii
import json
import logging
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

from app.config import settings
from app.services.driver_loader import (
    REQUIRED_METHODS,
    extract_driver_package,
    read_driver_metadata,
)
from app.services.driver_sandbox import execute_driver_method, extract_config_schema

logger = logging.getLogger(__name__)

# v1 validates recipes only (ADR 0005 decision point 8). The dry-run
# lifecycle sequence below is Hypervisor-specific; other connection types
# need their own sequence before this restriction can lift.
SUPPORTED_CONNECTION_TYPES = frozenset({"Hypervisor"})

# The order mirrors a real provisioning cycle so the transcript an admin
# reviews reads like the consumer's actual call sequence (ADR 0004).
LIFECYCLE_SEQUENCE = ("login", "create_instance", "status", "destroy_instance", "logout")

# Identifier fragments that mark an inline-credential assignment. Matched
# case-insensitively as substrings of the assigned name or dict key; only a
# non-empty string literal value trips the check, so `password = ""` and
# `password = context.get("HERD_password")` both stay legal.
_CREDENTIAL_NAME_FRAGMENTS = ("password", "passwd", "secret", "token", "api_key", "apikey")

# Modules the sandbox runner provides on sys.path at execution time; they are
# neither stdlib nor package-local but always importable in the subprocess.
# driver_transcript is how record_command transcripts reach the report the
# reviewing admin reads, so generated recipes are encouraged to use it.
_SANDBOX_PROVIDED_MODULES = frozenset({"driver_transcript"})


class PackageDecodeError(ValueError):
    """The request body could not be turned into a package archive."""


def _decode_package(package_b64: str) -> bytes:
    try:
        package_bytes = base64.b64decode(package_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise PackageDecodeError("package_b64 is not valid base64") from exc
    max_bytes = settings.validate_package_max_bytes
    if len(package_bytes) > max_bytes:
        raise PackageDecodeError(f"package exceeds the {max_bytes} byte validation limit")
    if not package_bytes:
        raise PackageDecodeError("package is empty")
    return package_bytes


def _parse_python_files(package_dir: Path) -> tuple[dict[Path, ast.AST], list[str]]:
    """Parse every .py file in the package. Returns (trees, parse_errors)."""
    trees: dict[Path, ast.AST] = {}
    errors: list[str] = []
    for py_file in sorted(package_dir.rglob("*.py")):
        rel = py_file.relative_to(package_dir)
        try:
            trees[rel] = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(rel))
        except (SyntaxError, UnicodeDecodeError, OSError) as exc:
            errors.append(f"{rel}: failed to parse: {exc}")
    return trees, errors


def _structural_errors(package_dir: Path, connection_type: str) -> list[str]:
    """AST-only structural validation: driver.py, class Driver, required methods.

    Deliberately does not import the module; import-time failures surface
    from the sandboxed steps instead, where arbitrary top-level code cannot
    touch this process.
    """
    errors: list[str] = []
    required = REQUIRED_METHODS.get(connection_type)
    if required is None:
        return [f"Unknown connection type: {connection_type}"]

    driver_py = package_dir / "driver.py"
    if not driver_py.exists():
        return ["Missing driver.py at package root"]

    try:
        tree = ast.parse(driver_py.read_text(encoding="utf-8"), filename="driver.py")
    except (SyntaxError, UnicodeDecodeError, OSError) as exc:
        return [f"driver.py failed to parse: {exc}"]

    driver_cls = next(
        (node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Driver"),
        None,
    )
    if driver_cls is None:
        return ["driver.py must define a top-level class named Driver"]

    defined = {
        node.name
        for node in driver_cls.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for method_name in required:
        if method_name not in defined:
            errors.append(f"Driver class is missing required method: {method_name}")
    return errors


def _import_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                continue  # relative import, package-local by construction
            if node.module:
                roots.add(node.module.split(".")[0])
    return roots


def _local_module_names(package_dir: Path) -> set[str]:
    names: set[str] = set()
    for entry in package_dir.iterdir():
        if entry.is_file() and entry.suffix == ".py":
            names.add(entry.stem)
        elif entry.is_dir():
            names.add(entry.name)
    return names


def _is_credential_name(name: str) -> bool:
    lowered = name.lower()
    return any(fragment in lowered for fragment in _CREDENTIAL_NAME_FRAGMENTS)


def _is_nonempty_str_constant(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value != ""


def _credential_literal_errors(rel: Path, tree: ast.AST) -> list[str]:
    """Flag credential-named targets assigned non-empty string literals.

    Covers plain and annotated assignments, call keywords
    (connect(password="...")), and dict literals ({"password": "..."}).
    Values that are not string constants (context lookups, concatenation)
    never trip this; the check is a narrow inline-secret heuristic, not a
    linter.
    """
    errors: list[str] = []

    def flag(name: str, line: int) -> None:
        errors.append(
            f"{rel}:{line}: credential-like name {name!r} is assigned an inline "
            "string literal; credentials must come from the context (password_keys)"
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _is_nonempty_str_constant(node.value):
            for target in node.targets:
                if isinstance(target, ast.Name) and _is_credential_name(target.id):
                    flag(target.id, node.lineno)
                elif isinstance(target, ast.Attribute) and _is_credential_name(target.attr):
                    flag(target.attr, node.lineno)
        elif (
            isinstance(node, ast.AnnAssign)
            and node.value is not None
            and _is_nonempty_str_constant(node.value)
            and isinstance(node.target, ast.Name)
            and _is_credential_name(node.target.id)
        ):
            flag(node.target.id, node.lineno)
        elif isinstance(node, ast.keyword):
            if node.arg and _is_credential_name(node.arg) and _is_nonempty_str_constant(node.value):
                flag(node.arg, node.value.lineno)
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (
                    isinstance(key, ast.Constant)
                    and isinstance(key.value, str)
                    and _is_credential_name(key.value)
                    and _is_nonempty_str_constant(value)
                ):
                    flag(key.value, value.lineno)
    return errors


def _policy_errors(package_dir: Path) -> list[str]:
    """The stricter generated-recipe contract (ADR 0005).

    - driver_metadata.json must exist, parse, and declare supports_dry_run
      true (the dry-run section depends on it, and DryRunRefused is the
      sandbox backstop).
    - Standard-library imports only in v1: no _deps/ vendoring, no
      requirements.txt, every absolute import resolves to the stdlib or to
      a module shipped inside the package.
    - No inline credential string literals.
    """
    errors: list[str] = []

    metadata_path = package_dir / "driver_metadata.json"
    if not metadata_path.exists():
        errors.append("driver_metadata.json is required for generated recipes")
    else:
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            metadata = None
            errors.append(f"driver_metadata.json failed to parse: {exc}")
        if isinstance(metadata, dict):
            if metadata.get("supports_dry_run") is not True:
                errors.append(
                    "driver_metadata.json must declare supports_dry_run: true; "
                    "generated recipes are validated through a sandboxed dry-run"
                )
        elif metadata is not None:
            errors.append("driver_metadata.json must be a JSON object")

    if (package_dir / "_deps").exists():
        errors.append("_deps/ vendoring is not allowed in generated recipes (stdlib only in v1)")
    if (package_dir / "requirements.txt").exists():
        errors.append("requirements.txt is not allowed in generated recipes (stdlib only in v1)")

    trees, parse_errors = _parse_python_files(package_dir)
    errors.extend(parse_errors)

    local_modules = _local_module_names(package_dir)
    allowed = set(sys.stdlib_module_names) | _SANDBOX_PROVIDED_MODULES
    for rel, tree in trees.items():
        for root in sorted(_import_roots(tree)):
            if root not in allowed and root not in local_modules:
                errors.append(
                    f"{rel}: import {root!r} is neither standard library nor "
                    "package-local (generated recipes are stdlib only in v1)"
                )
        errors.extend(_credential_literal_errors(rel, tree))

    return errors


def _synthetic_context() -> tuple[dict, set[str]]:
    """A representative Hypervisor recipe context with fake credentials.

    The endpoint uses the reserved .invalid TLD so a recipe that ignores
    dry_run and attempts wire I/O fails DNS resolution rather than reaching
    anything real. The password travels under password_keys so the child
    environment never carries it, same as production.
    """
    context = {
        "HERD_endpoint": "https://hypervisor.invalid:8006",
        "HERD_username": "herd-validation",
        "HERD_password": "herd-validation-credential",
        "HERD_request_id": str(uuid.uuid4()),
        "HERD_name": "herd-validation-instance",
        "HERD_image": "validation-image",
        "HERD_cpu": 1,
        "HERD_memory_mb": 512,
    }
    return context, {"HERD_password"}


def _method_passed(result: dict) -> bool:
    """Sandbox success AND no explicit driver-level failure verdict.

    Run-level status is sandbox-level ("the method executed"); the driver's
    own verdict lives in the output JSON (the #32 discipline), so both must
    agree for a validation pass.
    """
    if not result.get("success"):
        return False
    output = result.get("output")
    if isinstance(output, dict) and output.get("success") is False:
        return False
    return True


def _run_dry_run_lifecycle(package_dir: Path) -> dict:
    metadata = read_driver_metadata(package_dir)
    context, password_keys = _synthetic_context()
    methods: list[dict] = []
    passed = True
    instance_ref: str | None = None

    for action in LIFECYCLE_SEQUENCE:
        method_kwargs: dict = {}
        if action == "destroy_instance" and instance_ref is not None:
            method_kwargs["instance_ref"] = instance_ref
        result = execute_driver_method(
            str(package_dir),
            action=action,
            context=context,
            method_kwargs=method_kwargs or None,
            timeout=settings.validate_dry_run_timeout_seconds,
            dry_run=True,
            driver_metadata=metadata,
            password_keys=password_keys,
        )
        ok = _method_passed(result)
        passed = passed and ok
        output = result.get("output")
        if action == "create_instance" and isinstance(output, dict):
            ref = output.get("instance_ref")
            if isinstance(ref, str) and ref:
                instance_ref = ref
        methods.append(
            {
                "action": action,
                "passed": ok,
                "success": bool(result.get("success")),
                "output": output,
                "error": result.get("error"),
                "duration_ms": result.get("duration_ms"),
                "transcript": result.get("transcript") or [],
            }
        )

    return {"passed": passed, "methods": methods}


def _extract_schema_section(package_dir: Path) -> dict:
    result = extract_config_schema(str(package_dir))
    if not result.get("success"):
        return {"present": False, "schema": None, "error": result.get("error")}
    output = result.get("output") or {}
    if not output.get("has_schema"):
        return {"present": False, "schema": None, "error": None}
    schema = output.get("schema")
    if not isinstance(schema, dict):
        return {"present": False, "schema": None, "error": "config_schema() returned a non-dict"}
    return {"present": True, "schema": schema, "error": None}


def validate_package(package_b64: str, filename: str, connection_type: str) -> dict:
    """Run the full validation pipeline and return the report.

    Blocking (subprocess work); callers on the event loop wrap this in a
    thread. Raises PackageDecodeError for a body that cannot become an
    archive; every package-level defect lands in the report instead.
    """
    package_bytes = _decode_package(package_b64)

    tmp_dir = Path(tempfile.mkdtemp(prefix="herd_validate_"))
    try:
        try:
            extract_driver_package(package_bytes, filename, tmp_dir)
        except Exception as exc:
            return {
                "valid": False,
                "structural": {"passed": False, "errors": [f"Failed to extract package: {exc}"]},
                "policy": {"passed": False, "errors": []},
                "schema": {"present": False, "schema": None, "error": None},
                "dry_run": {"passed": False, "methods": [], "error": "not run"},
            }

        structural = _structural_errors(tmp_dir, connection_type)
        policy = _policy_errors(tmp_dir)

        # Only a statically clean package gets executed, even in the sandbox.
        if structural or policy:
            return {
                "valid": False,
                "structural": {"passed": not structural, "errors": structural},
                "policy": {"passed": not policy, "errors": policy},
                "schema": {"present": False, "schema": None, "error": None},
                "dry_run": {"passed": False, "methods": [], "error": "not run"},
            }

        schema = _extract_schema_section(tmp_dir)
        dry_run = _run_dry_run_lifecycle(tmp_dir)
        dry_run["error"] = None

        # A published schema is optional and never gates validity; the
        # structural and policy sections already passed to get here.
        return {
            "valid": dry_run["passed"],
            "structural": {"passed": True, "errors": []},
            "policy": {"passed": True, "errors": []},
            "schema": schema,
            "dry_run": dry_run,
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
