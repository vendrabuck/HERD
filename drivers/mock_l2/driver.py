"""Mock Layer 2 Switch driver for integration tests.

Hardware-free: every method returns a deterministic success result and records a
transcript line, so the execution sandbox can run it as a real subprocess
without a real switch. VLAN id allocation and per-fabric uniqueness live in HERD
(vlan_service.find_or_assign_vlan), not here; this driver just acknowledges each
operation, so it stays stateless.

Connection type: Layer 2 Switch
  login, logout, create_vlan(vlan_id), add_to_vlan(port, vlan_id, tag),
  remove_from_vlan(port, vlan_id), delete_vlan(vlan_id), status.

Failure injection (for the DLQ / retry / idempotency integration tests) is read
from the device field_data, which arrives HERD_-prefixed in the context dict:
  HERD_mock_fail_actions   comma-separated action names that return success=False
                           (drives the FAILED path)
  HERD_mock_raise_actions  comma-separated action names that raise (drives the
                           transient-NAK, and on exhaustion the DLQ, path)
  HERD_mock_sleep_ms       per-call sleep in milliseconds (to exercise the
                           sandbox action timeout)

dry_run is honored on every method (no state, results flagged simulated);
driver_metadata.json advertises supports_dry_run: true, which is binding. This
package is test infrastructure and is not meant for production use.
"""

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


class Driver:
    """Stateless, hardware-free Layer 2 switch driver for tests."""

    def __init__(self, context):
        self.context = context
        self.dry_run = bool(context.get("dry_run", False))
        # Failure-injection knobs, read from HERD_-prefixed field_data.
        self._fail_actions = _csv_set(context.get("HERD_mock_fail_actions"))
        self._raise_actions = _csv_set(context.get("HERD_mock_raise_actions"))
        self._sleep_ms = int(context.get("HERD_mock_sleep_ms", 0) or 0)

    # --- helpers ------------------------------------------------------------

    def _maybe_inject(self, action):
        """Apply configured failure injection for `action`.

        Returns a failure result dict to short-circuit the caller, raises for the
        transient path, or returns None when the action should proceed normally.
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

    def _ok(self, action, **output):
        """Build a success result, recording a transcript line."""
        if self.dry_run:
            record_command(action, response="(simulated)", exit_status="simulated")
            return {"success": True, "simulated": True, "output": output}
        record_command(action, response="(ok)")
        return {"success": True, "output": output}

    # --- Layer 2 contract ---------------------------------------------------

    def login(self):
        return self._maybe_inject("login") or self._ok("login")

    def logout(self):
        return self._maybe_inject("logout") or self._ok("logout")

    def create_vlan(self, vlan_id, **_):
        return self._maybe_inject("create_vlan") or self._ok("create_vlan", vlan_id=vlan_id)

    def add_to_vlan(self, port, vlan_id, tag, **_):
        return self._maybe_inject("add_to_vlan") or self._ok(
            "add_to_vlan", port=port, vlan_id=vlan_id, tag=tag
        )

    def remove_from_vlan(self, port, vlan_id, **_):
        return self._maybe_inject("remove_from_vlan") or self._ok(
            "remove_from_vlan", port=port, vlan_id=vlan_id
        )

    def delete_vlan(self, vlan_id, **_):
        return self._maybe_inject("delete_vlan") or self._ok("delete_vlan", vlan_id=vlan_id)

    def status(self):
        """Reachability check. Never raises (status must always answer)."""
        if "status" in self._fail_actions:
            return {"reachable": False, "error": "mock injected failure on status"}
        return {"reachable": True, "simulated": self.dry_run}
