"""Mock FRR-style L3 routing switch driver used by the golden-transcript suite.

Sibling of mock_ios: it translates the L3 contract methods into realistic
vtysh-style CLI bytes against an in-process MockTransport recorder, so the
test runs without network access AND the per-command transcript captures the
exact strings a real driver would emit. The golden tests pin those strings.

Route commands follow the FRR static-route grammar: a route with a next hop
renders as "ip route <destination> <next_hop> <interface>", and an interface
route (next_hop is None) renders as "ip route <destination> <interface>".

The driver honors context["dry_run"]: in simulation mode the MockTransport
is bypassed entirely, but record_command still fires with the same command
strings tagged exit_status="simulated".

Contract: L3 Switch (login, logout, configure_route, remove_route, status).
See docs/DRIVERS.md.
"""

try:
    from driver_transcript import record_command
except ImportError:

    def record_command(*args, **kwargs):
        pass


class _MockTransport:
    """Stand-in for paramiko/netmiko/etc. Records sends without I/O."""

    def __init__(self):
        self.sent: list[str] = []

    def send(self, line: str) -> str:
        self.sent.append(line)
        return "ok"


class Driver:
    """FRR-style Layer 3 routing switch driver, mock backend."""

    def __init__(self, context):
        self.context = context
        self.dry_run = bool(context.get("dry_run", False))
        self.transport = _MockTransport()

    def _emit(self, command: str, response: str = "ok") -> None:
        if self.dry_run:
            record_command(command, response="(simulated)", exit_status="simulated")
        else:
            actual = self.transport.send(command)
            record_command(command, response=actual or response)

    @staticmethod
    def _route_words(destination, next_hop, interface) -> str:
        if next_hop:
            return f"{destination} {next_hop} {interface}"
        return f"{destination} {interface}"

    def login(self):
        self._emit("enable", response="#")
        self._emit("configure terminal", response="(config)#")
        return {"success": True}

    def logout(self):
        self._emit("end", response="#")
        self._emit("exit", response="")
        return {"success": True}

    def configure_route(self, destination, next_hop, interface):
        self._emit(
            f"ip route {self._route_words(destination, next_hop, interface)}",
            response="(config)#",
        )
        return {
            "success": True,
            "destination": destination,
            "next_hop": next_hop,
            "interface": interface,
            "simulated": self.dry_run,
        }

    def remove_route(self, destination, next_hop, interface):
        self._emit(
            f"no ip route {self._route_words(destination, next_hop, interface)}",
            response="(config)#",
        )
        return {
            "success": True,
            "destination": destination,
            "next_hop": next_hop,
            "interface": interface,
            "simulated": self.dry_run,
        }

    def status(self):
        self._emit("do show ip route summary", response="IP routing table")
        return {"reachable": True}
