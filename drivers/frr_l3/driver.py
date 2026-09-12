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
Optional: HERD_port (default 22; blank means 22, see _parse_port).

dry_run is honored on every mutating method: when set, the commands are recorded via
record_command(... exit_status="simulated") and NO SSH connection is opened, so a dry
run never touches the wire. driver_metadata.json advertises supports_dry_run: true,
which is binding (see docs/DRIVERS.md).

Dialect mapping (verified live against the checked-in NOS test lab's frr node,
docs/NOS_LAB.md, 127.0.0.1:2224):
  configure_route  "ip route <destination> <next_hop>"          (next_hop given)
                   "ip route <destination> <interface>"          (next_hop is None)
  remove_route     the same line prefixed with "no "

Persistence, deliberately absent: this driver never calls `write memory`
(netmiko's save_config), unlike drivers/frr_mgmt's configure(). The routes it
installs are RESERVATION-SCOPED: the execution service installs them when a
reservation's wiring says so and removes them again on teardown or a fork
re-save, so persisting them into the startup config would outlive the
reservation that justified them and resurrect stale routes on the next daemon
restart. The running config is the intended lifetime, and reconciliation
after a restart is the execution service's job (a full reconcile against
cabling's intended set), not a startup-config side effect. Stated here and in
docs/DRIVERS.md so the asymmetry with frr_mgmt reads as a decision rather
than an omission.

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

The benign carve-out is ANCHORED at the start of the rendered line, never a
substring test (issue #779), and remove_route classifies EVERY "%" line in the
response rather than stopping at the first. Verified live 2026-09-12: vtysh
echoes the offending command back inside its own rejection ("% Unknown
command: <the command>"), so an unanchored test would call a genuine
rejection benign whenever the command text happened to contain the phrase.
The all-or-nothing rule follows docs/DRIVERS.md's "Classify ALL of a device's
complaints before deciding": a response is `already_absent` only when EVERY
"%" line in it is the benign marker, and a single genuine line anywhere in
the response (first, last, or between two benign ones) reports failure with
that genuine line as `error`.

Keep in sync with drivers/frr_mgmt/driver.py: the two packages share this
device, this transport, and this "%"-line dialect, but a driver package is
uploaded and cached standalone (docs/DRIVERS.md, "Package structure"), so a
cross-package import would not resolve in the execution sandbox. The
duplication is deliberate; when the detection or the benign carve-out
changes here, change it there too.
"""

try:
    from driver_transcript import record_command
except ImportError:  # running outside the execution sandbox (e.g. unit tests)

    def record_command(*args, **kwargs):
        pass


class DriverError(Exception):
    """Raised when a real (non-dry-run) operation against the device fails."""


def _parse_port(raw):
    """Return the SSH port for the raw HERD_port context value.

    Missing or blank means the SSH default, 22: an optional device field that
    is present but empty reaches the driver context as "" verbatim
    (services/execution/app/services/execution_service.py), and issue #780 is
    what happens when that is fed straight to int().

    Anything else that is not an integer raises DriverError, and the CALLER
    decides when that happens. This is why the parse lives here and is called
    from _connect() rather than from __init__: a constructor that raises
    takes down status() too, and status() must degrade to
    {"reachable": False} instead of raising (docs/DRIVERS.md, "Return
    values"). Keep in sync with drivers/frr_mgmt/driver.py.
    """
    if raw is None:
        return 22
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    text = str(raw).strip()
    if not text:
        return 22
    try:
        return int(text)
    except ValueError:
        raise DriverError("HERD_port must be an integer") from None


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
#
# Matched with startswith, never with `in` (issue #779): FRR echoes the
# offending command inside "% Unknown command: <the command>", so a command
# that merely CONTAINS this phrase would otherwise have its genuine rejection
# classified as benign. Cabling validates this driver's inputs as IP objects
# today, so the echo is not reachable from HERD's own call path, but the
# anchor is what makes that a defense in depth rather than a dependency on a
# caller three services away. Keep in sync with drivers/frr_mgmt/driver.py.
_ALREADY_ABSENT_MARKER = "% Refusing to remove a non-existent route"


def _find_error_lines(output):
    """Return every vtysh "%"-prefixed line in `output`, in order.

    Best-effort, not proof of success: see the module docstring's IMPORTANT
    LIMITATION note. A rejected command that prints no "%" line (verified live
    for a malformed next-hop address) contributes nothing here even though
    nothing was installed.

    Returns every match, not just the first (issue #779): a single vtysh
    command's response can span several lines, so a first-match scanner would
    let a benign "already absent" line hide a genuine rejection printed after
    it. docs/DRIVERS.md ("Classify ALL of a device's complaints before
    deciding") makes classifying all of them binding. Keep in sync with
    drivers/frr_mgmt/driver.py's helper of the same name; duplicated rather
    than imported because driver packages are loaded standalone in the
    execution sandbox.
    """
    return [line.strip() for line in output.splitlines() if line.strip().startswith("%")]


def _classify_error_lines(error_lines):
    """Partition `error_lines` into (genuine, benign) in their original order.

    "Benign" means only the one anchored marker above; everything else is
    genuine. Order is preserved, so a caller reporting "the first genuine
    failure" reports the first one that actually matters, not merely the
    first "%" line. Keep in sync with drivers/frr_mgmt/driver.py.
    """
    genuine = [line for line in error_lines if not line.startswith(_ALREADY_ABSENT_MARKER)]
    benign = [line for line in error_lines if line.startswith(_ALREADY_ABSENT_MARKER)]
    return genuine, benign


class Driver:
    """FRRouting Layer 3 switch driver, SSH-to-vtysh via netmiko."""

    def __init__(self, context):
        """Record the connection context. Deliberately never raises.

        Nothing here parses or validates: a constructor that raises for bad
        field_data makes status() a hard sandbox failure instead of
        {"reachable": False} (issue #780). HERD_port is parsed in _connect(),
        alongside the missing-host/login check that already lives there.
        """
        self.context = context
        self.dry_run = bool(context.get("dry_run", False))
        self.host = context.get("HERD_ip") or context.get("HERD_ip_address")
        self.username = context.get("HERD_login") or context.get("HERD_username")
        self.password = context.get("HERD_password")
        self.port_raw = context.get("HERD_port")
        self._conn = None

    # --- connection helpers -------------------------------------------------

    def _connect(self):
        """Open a netmiko session to vtysh. Never called in dry-run.

        The missing-connection-params guard lives here (rather than duplicated in
        every method that needs a live session) so login(), configure_route(), and
        remove_route() all raise a clean DriverError instead of an opaque netmiko/
        connection-library exception; status() still degrades to
        {"reachable": False} instead of raising, since it catches everything this
        method can throw. HERD_port is parsed here for the same reason
        (issue #780): a bad port value must surface from the call that needed
        the connection, not from the constructor.
        """
        if not self.host or not self.username:
            raise DriverError("missing HERD_ip / HERD_login for the device")
        from netmiko import ConnectHandler

        port = _parse_port(self.port_raw)

        if self._conn is None:
            self._conn = ConnectHandler(
                device_type="cisco_ios",  # vtysh speaks IOS-style config/show
                host=self.host,
                username=self.username,
                password=self.password,
                port=port,
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
        sections: ANY "%" line in the output is a genuine rejection and reports
        {"success": False, "error": <the first such line>}; anything else
        (including a silent no-op re-apply of an already-configured route)
        reports {"success": True}. The benign "already absent" carve-out is
        remove_route's alone: this direction has no no-op the device
        complains about.
        """
        command = _route_command("ip route", destination, next_hop, interface)
        if self.dry_run:
            record_command(command, response="(simulated)", exit_status="simulated")
            return {"success": True, "simulated": True}
        conn = self._connect()
        output = conn.send_config_set([command])
        error_lines = _find_error_lines(output)
        if error_lines:
            record_command(command, response=output, exit_status="error")
            return {"success": False, "error": error_lines[0], "output": output}
        record_command(command, response=output)
        return {"success": True, "output": output}

    def remove_route(self, destination, next_hop, interface, **_):
        """Remove one static route.

        See the module docstring's "Error detection" and "Idempotency decision"
        sections: the device's anchored "already absent" warning is the one
        "%" line deliberately treated as success (the desired end state
        already holds); every other "%" line is a genuine rejection and
        reports {"success": False}.

        EVERY "%" line in the response is classified, not just the first
        (issue #779): the result is {"success": True, "already_absent": True}
        only when every line found is the benign marker, and a single genuine
        line anywhere in the response reports {"success": False} with the
        FIRST GENUINE line as `error`, whether it arrived before or after a
        benign one.
        """
        command = _route_command("no ip route", destination, next_hop, interface)
        if self.dry_run:
            record_command(command, response="(simulated)", exit_status="simulated")
            return {"success": True, "simulated": True}
        conn = self._connect()
        output = conn.send_config_set([command])
        error_lines = _find_error_lines(output)
        genuine, benign = _classify_error_lines(error_lines)
        if genuine:
            record_command(command, response=output, exit_status="error")
            return {"success": False, "error": genuine[0], "output": output}
        if benign:
            record_command(command, response=output)
            return {"success": True, "output": output, "already_absent": True}
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
