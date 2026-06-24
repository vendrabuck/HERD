"""Mock Layer 1 Switch driver for integration tests.

Hardware-free L1 cross-connect driver: connect_ports / disconnect_ports patch a
port pair through the matrix. Like the mock L2 driver (drivers/mock_l2) it is
stateless and just acknowledges each op, so the execution sandbox can run it as a
real subprocess without a real switch.

Connection type: Layer 1 Switch
  login, logout, connect_ports(port_a, port_b),
  disconnect_ports(port_a, port_b), status.

Failure injection (for the DLQ / retry / idempotency integration tests) is read
from the device field_data, which arrives HERD_-prefixed in the context dict:
  HERD_mock_fail_actions   comma-separated action names that return success=False
  HERD_mock_raise_actions  comma-separated action names that raise
  HERD_mock_sleep_ms       per-call sleep in milliseconds

dry_run is honored on every method (results flagged simulated);
driver_metadata.json advertises supports_dry_run: true. Test infrastructure, not
for production use.
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
    """Stateless, hardware-free Layer 1 cross-connect driver for tests."""

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

        Returns a failure result dict to short-circuit, raises for the transient
        path, or returns None when the action should proceed normally.
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

    # --- Layer 1 contract ---------------------------------------------------

    def login(self):
        return self._maybe_inject("login") or self._ok("login")

    def logout(self):
        return self._maybe_inject("logout") or self._ok("logout")

    def connect_ports(self, port_a, port_b, **_):
        return self._maybe_inject("connect_ports") or self._ok(
            "connect_ports", port_a=port_a, port_b=port_b
        )

    def disconnect_ports(self, port_a, port_b, **_):
        return self._maybe_inject("disconnect_ports") or self._ok(
            "disconnect_ports", port_a=port_a, port_b=port_b
        )

    def status(self):
        """Reachability check. Never raises (status must always answer)."""
        if "status" in self._fail_actions:
            return {"reachable": False, "error": "mock injected failure on status"}
        return {"reachable": True, "simulated": self.dry_run}
