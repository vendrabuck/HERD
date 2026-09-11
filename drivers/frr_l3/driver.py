"""FRRouting Layer 3 Switch driver: installs/removes static routes over SSH via vtysh.

Connection type: Layer 3 Switch (login, logout, configure_route, remove_route, status).

Same device, same transport, and same netmiko platform as drivers/frr_mgmt: the
router runs sshd with a login user whose shell is /usr/bin/vtysh, so an SSH session
lands directly in the FRR CLI, and vtysh accepts Cisco-IOS-style commands, so
netmiko's cisco_ios platform drives it. This package implements the narrower Layer 3
Switch contract instead of Management: one static route per configure_route/
remove_route call (docs/DRIVERS.md, "Layer 3 Switch driver contract"), rather than
raw config lines.

Connection params come from the device field_data as HERD_-prefixed context keys:
  HERD_ip       host/IP the execution service can reach (e.g. 127.0.0.1)
  HERD_login    SSH username (its login shell is vtysh)
  HERD_password SSH password
Optional: HERD_port (default 22).

dry_run is honored on every mutating method: when set, the commands are recorded via
record_command(... exit_status="simulated") and NO SSH connection is opened, so a dry
run never touches the wire. driver_metadata.json advertises supports_dry_run: true,
which is binding (see docs/DRIVERS.md).

Dialect mapping (verified live against the checked-in NOS test lab's frr node,
docs/NOS_LAB.md, 127.0.0.1:2224):
  configure_route  "ip route <destination> <next_hop>"          (next_hop given)
                   "ip route <destination> <interface>"          (next_hop is None)
  remove_route     the same line prefixed with "no "

Error detection (load-bearing; see docs/DRIVERS.md's "FRR reference driver" note):
vtysh reports a rejected command as an output line starting with "%", e.g.
"% Unknown command: ip route 999.999.999.0/24 172.17.0.1" for a malformed
destination. netmiko does NOT raise for this: send_config_set returns normally
with the error text embedded in the output. This driver scans for a "%" line
(as does drivers/frr_mgmt/driver.py's configure()/backup(), fixed alongside it
for issue #771), because a false {"success": True} here is a silent
provisioning failure: the execution service keys ledger state on the driver's
returned payload, not on transport health (docs/DRIVERS.md, "Driver-call success
is keyed on the DRIVER RESULT payload"). A "%" line (other than the one benign
case below) means {"success": False, "error": <the offending line>}.

IMPORTANT LIMITATION, stated plainly rather than papered over: this is
best-effort. Not every rejected command prints a "%" line. Verified live: `ip
route 203.0.113.8/30 999.1.1.1` (a syntactically invalid next hop) produces NO
output at all and installs nothing, yet there is no error text to detect. So
{"success": True} means "the device did not report a failure", not "the route
is proven to be in the RIB". Nothing in this driver parses `show ip route` to
close that gap, and nothing should: verifying a driver's own work through the
driver's own read path is the exact anti-pattern the live NOS-lab tests exist to
avoid (tests/nos_lab/test_frr_l3_driver_live.py verifies independently via a
separate `docker exec ... vtysh` call instead).

Idempotency decision (evidence, not assumption; see the report for the human-facing
summary):
  - configure_route re-sending an already-configured route is a silent no-op on the
    real device: FRR accepts a duplicate `ip route` line with no error (no "%"
    line) and the output is identical to the first apply. No special handling is
    needed in this driver for the "install" direction; FRR itself is already
    idempotent there.
  - remove_route on an already-removed route DOES print a "%" line on the real
    device, "% Refusing to remove a non-existent route", but this one case is
    deliberately treated as success rather than failure: removing an
    already-absent route converges to the same desired state (the route is
    gone), which is exactly what HERD's execution service needs when a
    wiring-changed/deprovision event is redelivered or a retry channel
    re-drives a row. Every OTHER "%" line from remove_route is a genuine
    failure and is reported as {"success": False}, same as configure_route.
  Net effect: both configure_route and remove_route are idempotent under
  redelivery, mirroring the Layer 2 contract's explicit idempotent create_vlan
  requirement, even though docs/DRIVERS.md does not currently state this rule for
  L3, while a genuine rejection (any other "%" line) is never reported as
  success.
"""

try:
    from driver_transcript import record_command
except ImportError:  # running outside the execution sandbox (e.g. unit tests)

    def record_command(*args, **kwargs):
        pass


class DriverError(Exception):
    """Raised when a real (non-dry-run) operation against the device fails."""


def _route_command(verb, destination, next_hop, interface):
    """Render the vtysh route command line for `verb` ("ip route" / "no ip route").

    An explicit next_hop is a next-hop route: "<verb> <destination> <next_hop>",
    FRR resolves the egress interface itself. next_hop=None is an interface route:
    "<verb> <destination> <interface>". Verified live: FRR neither wants nor
    accepts both a next-hop and an interface on the same line for this usage.
    """
    if next_hop is None:
        return f"{verb} {destination} {interface}"
    return f"{verb} {destination} {next_hop}"


# The one "%" line that means "already in the desired state", not "rejected".
# Only meaningful for remove_route; configure_route treats every "%" line as a
# genuine failure (see the module docstring's "Error detection" section).
_ALREADY_ABSENT_MARKER = "Refusing to remove a non-existent route"


def _find_error_line(output):
    """Return the first vtysh "%"-prefixed error line in `output`, or None.

    Best-effort, not proof of success: see the module docstring's IMPORTANT
    LIMITATION note. A rejected command that prints no "%" line (verified live
    for a malformed next-hop address) returns None here even though nothing was
    installed.
    """
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("%"):
            return stripped
    return None


class Driver:
    """FRRouting Layer 3 switch driver, SSH-to-vtysh via netmiko."""

    def __init__(self, context):
        self.context = context
        self.dry_run = bool(context.get("dry_run", False))
        self.host = context.get("HERD_ip") or context.get("HERD_ip_address")
        self.username = context.get("HERD_login") or context.get("HERD_username")
        self.password = context.get("HERD_password")
        self.port = int(context.get("HERD_port", 22))
        self._conn = None

    # --- connection helpers -------------------------------------------------

    def _connect(self):
        """Open a netmiko session to vtysh. Never called in dry-run.

        The missing-connection-params guard lives here (rather than duplicated in
        every method that needs a live session) so login(), configure_route(), and
        remove_route() all raise a clean DriverError instead of an opaque netmiko/
        connection-library exception; status() still degrades to
        {"reachable": False} instead of raising, since it catches everything this
        method can throw.
        """
        if not self.host or not self.username:
            raise DriverError("missing HERD_ip / HERD_login for the device")
        from netmiko import ConnectHandler

        if self._conn is None:
            self._conn = ConnectHandler(
                device_type="cisco_ios",  # vtysh speaks IOS-style config/show
                host=self.host,
                username=self.username,
                password=self.password,
                port=self.port,
                fast_cli=False,
            )
        return self._conn

    def _disconnect(self):
        if self._conn is not None:
            try:
                self._conn.disconnect()
            finally:
                self._conn = None

    # --- Layer 3 Switch contract ---------------------------------------------

    def login(self):
        """Open the session (no-op in dry-run; the vtysh login is implicit)."""
        if self.dry_run:
            record_command(
                f"ssh {self.username}@{self.host}",
                response="(simulated)",
                exit_status="simulated",
            )
            return {"success": True, "simulated": True}
        self._connect()
        record_command(f"ssh {self.username}@{self.host}", response="(connected to vtysh)")
        return {"success": True}

    def logout(self):
        if self.dry_run:
            record_command("exit", response="(simulated)", exit_status="simulated")
            return {"success": True, "simulated": True}
        self._disconnect()
        record_command("exit", response="(disconnected)")
        return {"success": True}

    def configure_route(self, destination, next_hop, interface, **_):
        """Install one static route.

        See the module docstring's "Error detection" and "Idempotency decision"
        sections: a "%" line in the output is a genuine rejection and reports
        {"success": False}; anything else (including a silent no-op re-apply of
        an already-configured route) reports {"success": True}.
        """
        command = _route_command("ip route", destination, next_hop, interface)
        if self.dry_run:
            record_command(command, response="(simulated)", exit_status="simulated")
            return {"success": True, "simulated": True}
        conn = self._connect()
        output = conn.send_config_set([command])
        error_line = _find_error_line(output)
        if error_line is not None:
            record_command(command, response=output, exit_status="error")
            return {"success": False, "error": error_line, "output": output}
        record_command(command, response=output)
        return {"success": True, "output": output}

    def remove_route(self, destination, next_hop, interface, **_):
        """Remove one static route.

        See the module docstring's "Error detection" and "Idempotency decision"
        sections: the device's "already absent" warning is the one "%" line
        deliberately treated as success (the desired end state already holds);
        every other "%" line is a genuine rejection and reports
        {"success": False}.
        """
        command = _route_command("no ip route", destination, next_hop, interface)
        if self.dry_run:
            record_command(command, response="(simulated)", exit_status="simulated")
            return {"success": True, "simulated": True}
        conn = self._connect()
        output = conn.send_config_set([command])
        error_line = _find_error_line(output)
        if error_line is not None:
            if _ALREADY_ABSENT_MARKER in error_line:
                record_command(command, response=output)
                return {"success": True, "output": output, "already_absent": True}
            record_command(command, response=output, exit_status="error")
            return {"success": False, "error": error_line, "output": output}
        record_command(command, response=output)
        return {"success": True, "output": output}

    def status(self):
        """Reachability check: open a session and read the version banner."""
        if self.dry_run:
            record_command("show version", response="(simulated)", exit_status="simulated")
            return {"reachable": True, "simulated": True}
        try:
            conn = self._connect()
            version = conn.send_command("show version")
            record_command("show version", response=version)
            reachable = "FRRouting" in version or "FRR" in version
            return {"reachable": reachable}
        except Exception as e:  # noqa: BLE001 - status must not raise; report unreachable
            record_command("show version", response=str(e), exit_status="error")
            return {"reachable": False, "error": str(e)}
