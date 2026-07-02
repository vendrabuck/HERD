"""Mock Layer 3 Switch driver for integration tests.

Hardware-free: every method returns a deterministic success result and records a
transcript line, so the execution sandbox can run it as a real subprocess
without a real router. Route selection and pinning live in HERD (the L3
route-assignment flow), not here; this driver just acknowledges each operation,
so it stays stateless.

Connection type: Layer 3 Switch
  login, logout, configure_route(destination, next_hop, interface),
  remove_route(destination, next_hop, interface), status.
next_hop may be None (an interface route); the recorded command then omits it.
An optional configure(**config) is included so inventory config-apply jobs stay
demonstrable against this driver.

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


def _route_command(verb, destination, next_hop, interface):
    """Render a route transcript line; interface routes (next_hop None) omit the hop."""
    if next_hop is None:
        return f"{verb} {destination} {interface}"
    return f"{verb} {destination} {next_hop} {interface}"


class Driver:
    """Stateless, hardware-free Layer 3 switch driver for tests."""

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

    def _ok(self, action, command=None, **output):
        """Build a success result, recording a transcript line (`command` overrides it)."""
        line = command if command is not None else action
        if self.dry_run:
            record_command(line, response="(simulated)", exit_status="simulated")
            return {"success": True, "simulated": True, "output": output}
        record_command(line, response="(ok)")
        return {"success": True, "output": output}

    # --- Layer 3 contract ---------------------------------------------------

    def login(self):
        return self._maybe_inject("login") or self._ok("login")

    def logout(self):
        return self._maybe_inject("logout") or self._ok("logout")

    def configure_route(self, destination, next_hop, interface, **_):
        return self._maybe_inject("configure_route") or self._ok(
            "configure_route",
            command=_route_command("ip route", destination, next_hop, interface),
            destination=destination,
            next_hop=next_hop,
            interface=interface,
        )

    def remove_route(self, destination, next_hop, interface, **_):
        return self._maybe_inject("remove_route") or self._ok(
            "remove_route",
            command=_route_command("no ip route", destination, next_hop, interface),
            destination=destination,
            next_hop=next_hop,
            interface=interface,
        )

    def configure(self, **config):
        """Optional extra beyond the L3 contract: acknowledge a config-apply job."""
        return self._maybe_inject("configure") or self._ok("configure", config=config)

    def status(self):
        """Reachability check. Never raises (status must always answer)."""
        if "status" in self._fail_actions:
            return {"reachable": False, "error": "mock injected failure on status"}
        return {"reachable": True, "simulated": self.dry_run}
