"""Mock Hypervisor recipe driver for integration tests (ADR 0004, issue #32).

Hardware-free: every method returns a deterministic acknowledgement and records
a transcript line, so the execution sandbox can run it as a real subprocess
without a real hypervisor. It implements the Hypervisor connection-type recipe
contract the execution consumer drives for dynamic resources:

  login, logout, create_instance, destroy_instance, status.

create_instance names its fake instance from context HERD_request_id, which is
the redelivery-idempotency contract for recipe authors: a retried create for the
same request MUST resolve to the same instance rather than leak a duplicate. It
returns the shape the consumer's _recipe_reported_success and instance-ref
extraction expect (nats_consumer.py):

  {"success": True, "instance_ref": "<str>", "field_data": {<attrs>}}

field_data carries deterministic instance attributes (a management address
derived from the request id, plus an image echo when the template supplies
HERD_image), which inventory merges into the materialized device's field_data.
destroy_instance is idempotent per the ADR: destroying an already-absent
instance still returns success (this mock is stateless, so every destroy is an
acknowledgement).

Failure injection (for the FAILED / DLQ / retry integration tests) is read from
the context dict. For a dynamic template there is no device row yet, so these
arrive as template field DEFAULTS (HERD_-prefixed), not device field_data:
  HERD_mock_fail_actions   comma-separated action names that return
                           success=False (drives the FAILED path)
  HERD_mock_raise_actions  comma-separated action names that raise (drives the
                           transient-NAK, and on exhaustion the DLQ, path)
  HERD_mock_sleep_ms       per-call sleep in milliseconds (to exercise the
                           recipe action timeout and the timeout backstop)

dry_run is honored on every method (no state, results flagged simulated);
driver_metadata.json advertises supports_dry_run: true, which is binding. This
package is test infrastructure and is not meant for production use.
"""

import hashlib

try:
    from driver_transcript import record_command
except ImportError:  # running outside the execution sandbox (e.g. unit tests)

    def record_command(*args, **kwargs):
        pass


def _csv_set(value):
    """Parse a comma-separated context value into a set of action names."""
    if not value:
        return set()
    return {item.strip() for item in str(value).split(",") if item.strip()}


def _derive_mgmt_address(request_id):
    """Deterministic fake management address for a request id.

    Same request_id, same address: the instance attributes must be stable
    across a redelivered create, exactly like the instance_ref.
    """
    digest = hashlib.sha256(str(request_id).encode()).digest()
    return f"10.66.{digest[0] % 254 + 1}.{digest[1] % 254 + 1}"


class Driver:
    """Stateless, hardware-free Hypervisor recipe driver for tests."""

    def __init__(self, context):
        self.context = context
        self.dry_run = bool(context.get("dry_run", False))
        # Failure-injection knobs; for dynamic templates these come from the
        # template's field defaults (there is no device field_data yet).
        self._fail_actions = _csv_set(context.get("HERD_mock_fail_actions"))
        self._raise_actions = _csv_set(context.get("HERD_mock_raise_actions"))
        self._sleep_ms = int(context.get("HERD_mock_sleep_ms", 0) or 0)

    # --- helpers ------------------------------------------------------------

    def _maybe_inject(self, action):
        """Apply configured failure injection for `action`.

        Returns a failure result dict to short-circuit the caller, raises for
        the transient path, or returns None when the action should proceed.
        """
        if self._sleep_ms:
            import time

            time.sleep(self._sleep_ms / 1000.0)
        if action in self._raise_actions:
            raise RuntimeError(f"mock injected raise on {action}")
        if action in self._fail_actions:
            record_command(action, response="(injected failure)", exit_status="error")
            return {"success": False, "error": f"mock injected failure on {action}"}
        return None

    def _flag_simulated(self, result):
        if self.dry_run:
            result["simulated"] = True
        return result

    def _record_op(self, command):
        """Record a transcript line, flagged simulated under dry_run."""
        if self.dry_run:
            record_command(command, response="(simulated)", exit_status="simulated")
        else:
            record_command(command, response="(ok)")

    # --- Hypervisor recipe contract ------------------------------------------

    def login(self):
        injected = self._maybe_inject("login")
        if injected is not None:
            return injected
        self._record_op("login")
        return self._flag_simulated({"success": True, "output": {}})

    def logout(self):
        injected = self._maybe_inject("logout")
        if injected is not None:
            return injected
        self._record_op("logout")
        return self._flag_simulated({"success": True, "output": {}})

    def create_instance(self, **_):
        """Materialize a fake instance, named deterministically from the request id.

        The consumer reads instance_ref and field_data from this return value
        (top-level keys) and _recipe_reported_success requires success=True, so
        this shape is the contract; do not nest it under "output".
        """
        injected = self._maybe_inject("create_instance")
        if injected is not None:
            return injected

        request_id = self.context.get("HERD_request_id")
        if not request_id:
            record_command(
                "create_instance", response="(missing HERD_request_id)", exit_status="error"
            )
            return {
                "success": False,
                "error": "context HERD_request_id is required to name the instance",
            }

        instance_ref = f"mock-vm-{request_id}"
        field_data = {"mgmt_address": _derive_mgmt_address(request_id)}
        image = self.context.get("HERD_image")
        if image:
            field_data["image"] = image

        self._record_op(f"create instance {instance_ref}")
        return self._flag_simulated(
            {"success": True, "instance_ref": instance_ref, "field_data": field_data}
        )

    def destroy_instance(self, instance_ref=None, **_):
        """Destroy an instance; idempotent per the ADR contract.

        This mock is stateless, so destroying any instance, including one that
        was never created or is already gone, acknowledges with success=True.
        """
        injected = self._maybe_inject("destroy_instance")
        if injected is not None:
            return injected
        self._record_op(f"destroy instance {instance_ref}")
        return self._flag_simulated({"success": True, "instance_ref": instance_ref})

    def status(self):
        """Reachability check. Never raises (status must always answer)."""
        if "status" in self._fail_actions:
            return {"reachable": False, "error": "mock injected failure on status"}
        return {"reachable": True, "simulated": self.dry_run}
