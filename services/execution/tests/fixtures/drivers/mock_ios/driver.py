"""Mock Cisco IOS-style L2 switch driver used by the golden-transcript suite.

Unlike the seed drivers under seed_devices.py (which are pure no-ops that
return success without doing any work), this driver actually translates the
L2 contract methods into realistic IOS CLI bytes. It talks to an in-process
MockTransport recorder instead of opening a socket, so the test runs without
network access AND the per-command transcript captures the exact strings a
real driver would emit. The golden tests pin those strings.

The driver honors context["dry_run"]: in simulation mode the MockTransport
is bypassed entirely, but record_command still fires with the same command
strings tagged exit_status="simulated".

Contract: L2 Switch (login, logout, create_vlan, add_to_vlan, remove_from_vlan,
delete_vlan, status). See docs/DRIVERS.md.
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
    """Cisco Catalyst-style Layer 2 switch driver, mock backend."""

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

    def login(self):
        self._emit("enable", response="#")
        self._emit("configure terminal", response="(config)#")
        return {"success": True}

    def logout(self):
        self._emit("end", response="#")
        self._emit("exit", response="")
        return {"success": True}

    def create_vlan(self, vlan_id):
        self._emit(f"vlan {vlan_id}", response="(config-vlan)#")
        self._emit(" exit", response="(config)#")
        return {"success": True, "vlan_id": vlan_id, "simulated": self.dry_run}

    def add_to_vlan(self, port, vlan_id, tag="tagged"):
        self._emit(f"interface {port}", response="(config-if)#")
        if tag == "tagged":
            self._emit("switchport mode trunk", response="(config-if)#")
            self._emit(
                f"switchport trunk allowed vlan add {vlan_id}",
                response="(config-if)#",
            )
        else:
            self._emit("switchport mode access", response="(config-if)#")
            self._emit(
                f"switchport access vlan {vlan_id}",
                response="(config-if)#",
            )
        self._emit(" exit", response="(config)#")
        return {
            "success": True,
            "port": port,
            "vlan_id": vlan_id,
            "tag": tag,
            "simulated": self.dry_run,
        }

    def remove_from_vlan(self, port, vlan_id):
        self._emit(f"interface {port}", response="(config-if)#")
        self._emit(
            f"switchport trunk allowed vlan remove {vlan_id}",
            response="(config-if)#",
        )
        self._emit(" exit", response="(config)#")
        return {
            "success": True,
            "port": port,
            "vlan_id": vlan_id,
            "simulated": self.dry_run,
        }

    def delete_vlan(self, vlan_id):
        self._emit(f"no vlan {vlan_id}", response="(config)#")
        return {"success": True, "vlan_id": vlan_id, "simulated": self.dry_run}

    def status(self):
        self._emit("do show version", response="Cisco IOS Software")
        return {"reachable": True}
