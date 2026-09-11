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

Idempotency decision (evidence, not assumption; see the report for the human-facing
summary):
  - configure_route re-sending an already-configured route is a silent no-op on the
    real device: FRR accepts a duplicate `ip route` line with no error and the
    output is identical to the first apply. No special handling is needed in this
    driver for the "install" direction; FRR itself is already idempotent there.
  - remove_route on an already-removed route prints a benign CLI warning on the
    real device, "% Refusing to remove a non-existent route", but netmiko does not
    raise for it and the session/config mode stay healthy. This driver, like
    drivers/frr_mgmt, does not parse command output for embedded CLI errors, so
    that warning surfaces only in the recorded transcript and the call still
    reports {"success": True}: removing an already-absent route converges to the
    same desired state (the route is gone), which is exactly what HERD's
    execution service needs when a wiring-changed/deprovision event is
    redelivered or a retry channel re-drives a row.
  Net effect: both configure_route and remove_route are idempotent under
  redelivery, mirroring the Layer 2 contract's explicit idempotent create_vlan
  requirement, even though docs/DRIVERS.md does not currently state this rule for
  L3 (see docs/DRIVERS.md's new "FRR reference driver" note).
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
        """Install one static route. See the module docstring for idempotency."""
        command = _route_command("ip route", destination, next_hop, interface)
        if self.dry_run:
            record_command(command, response="(simulated)", exit_status="simulated")
            return {"success": True, "simulated": True}
        conn = self._connect()
        output = conn.send_config_set([command])
        record_command(command, response=output)
        return {"success": True, "output": output}

    def remove_route(self, destination, next_hop, interface, **_):
        """Remove one static route. See the module docstring for idempotency."""
        command = _route_command("no ip route", destination, next_hop, interface)
        if self.dry_run:
            record_command(command, response="(simulated)", exit_status="simulated")
            return {"success": True, "simulated": True}
        conn = self._connect()
        output = conn.send_config_set([command])
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
