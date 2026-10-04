"""Hypervisor recipe result rules (ADR 0004, issue #937).

One home for how a create_instance or destroy_instance result is judged, so the
NATS consumer (which acts on it) and the package validator (which approves an
AI-drafted recipe before it ever runs) apply the SAME rule. A validator looser
than the consumer would pass a recipe that then fails every real provision.
"""

from app.services.execution_service import driver_result_failed


def recipe_reported_success(result: dict) -> bool:
    """True when both the sandbox ran and the recipe's own success flag is set.

    Built on the shared ``driver_result_failed`` rule with one STRICTER delta:
    create_instance and destroy_instance must positively acknowledge with
    {"success": True, ...}, so a missing ``success`` key counts as failure
    here, where ``driver_result_failed``'s bare-data posture counts it as
    success. The delta is deliberate; do not swap one helper for the other
    (a recipe that never acknowledges an instance create must not be treated
    as provisioned). Login/logout carry no such flag, so callers check
    result["success"] directly for those.
    """
    failed, _ = driver_result_failed(result)
    if failed:
        return False
    output = result.get("output")
    return isinstance(output, dict) and bool(output.get("success"))


def created_instance_ref(result: dict) -> str | None:
    """The instance_ref a successful create_instance returned, or None.

    instance_ref is a required key of create_instance's result (docs/DRIVERS.md):
    it is the only handle a teardown can destroy by. A create that reports
    success with no instance_ref, an empty one, or a non-string one is treated
    as a failed create (issue #937): the consumer leaves the ledger row CREATING
    for a keyed teardown, and the package validator fails the draft, instead of
    an ACTIVE row carrying a live instance nothing can address.
    """
    output = result.get("output")
    if not isinstance(output, dict):
        return None
    ref = output.get("instance_ref")
    if isinstance(ref, str) and ref.strip():
        return ref
    return None
