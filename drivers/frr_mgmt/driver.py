"""FRRouting Management driver: configures an FRR router over SSH via vtysh.

Connection type: Management (login, logout, configure, backup, status).

The router runs sshd with a login user whose shell is /usr/bin/vtysh, so an SSH
session lands directly in the FRR CLI. vtysh accepts Cisco-IOS-style commands
(`configure terminal`, `end`, `show ...`), so netmiko's cisco_ios platform drives
it: send_config_set enters and exits config mode for us.

Connection params come from the device field_data as HERD_-prefixed context keys:
  HERD_ip       host/IP the execution service can reach (e.g. 10.99.0.11)
  HERD_login    SSH username (its login shell is vtysh)
  HERD_password SSH password
Optional: HERD_port (default 22; blank means 22, see _parse_port).

dry_run is honored on every mutating method: when set, the commands are recorded
via record_command(... exit_status="simulated") and NO SSH connection is opened,
so a dry run never touches the wire. driver_metadata.json advertises
supports_dry_run: true, which is binding (see docs/DRIVERS.md).

Error detection (load-bearing; see docs/DRIVERS.md's "A driver must report a
device rejection as a failure" section): vtysh reports a rejected command as
an output line starting with "%", e.g. "% Unknown command: ip route
999.999.999.0/24 172.17.0.1" for a malformed destination. netmiko does NOT
raise for this: send_config_set and send_command return normally with the
error text embedded in the output. Before issue #771 this driver never
inspected that output, so configure() and backup() always reported
{"success": True} regardless of what the device actually did, which let a
rejected config land in HERD's wiring/config-apply state as if it had been
applied. configure() and backup() now both scan for "%" lines and report
{"success": False, "error": <the offending line>, "output": <the full
output>} instead of trusting the absence of a raised exception, the same
technique drivers/frr_l3/driver.py uses for configure_route/remove_route.

Judged by desired end state, not by whether the device complained (the
general rule; see docs/DRIVERS.md's "A driver must report a device rejection
as a failure" section): configure() treats a "%" line as benign, not a
failure, exactly when the operation's goal already holds despite the
device's wording. Verified live: "no ip route <destination> <next_hop>"
against an already-absent route prints "% Refusing to remove a
non-existent route", the SAME line drivers/frr_l3's remove_route treats as
an idempotent success, and for the same reason, the route being removed is
already gone, which is what the caller wanted. This is a concrete instance
of a device-specific, driver-specific decision; it is not a rule that
transfers to other vendors or other commands by pattern-matching the string.

That carve-out is ANCHORED at the start of the rendered line, never a
substring test anywhere in it (issue #779). Verified live 2026-09-12: vtysh
echoes the offending command back inside its own rejection, so the config
line `ip route Refusing to remove a non-existent route` comes back as
"% Unknown command: ip route Refusing to remove a non-existent route", and
an unanchored `marker in line` test classified that genuine rejection as
benign and reported success. The published config schema accepts any string
up to 512 characters, so nothing upstream filters such a line out; the
anchor is the whole defense.

configure() classifies EVERY "%" line in the batch's output, not just the
first: it collects every line, partitions them into genuine failures and the
one known-benign marker above, and reports failure (with the FIRST GENUINE
line as `error`) if any genuine failure is present, success otherwise. This
matters specifically because configure() accepts an arbitrary BATCH of vtysh
lines in one call, unlike frr_l3's remove_route, which is always exactly one
line: stopping at the first "%" line, as an earlier version of this driver
did, would let a benign line that happens to come first in the output hide a
genuine failure on a LATER line in the same batch. Any benign lines found are
still surfaced, under "benign_warnings" in a successful result, so an
operator can see what the device said even though it did not change the
outcome.

Persistence is classified too, not assumed (issue #779). configure() ends by
calling netmiko's save_config() ("write memory"), and vtysh reports a FAILED
save with no "%" line at all and exit status 0, so the "%" scan above cannot
see it. Verified live 2026-09-12 against the NOS test lab node
(docs/NOS_LAB.md): a successful save prints one "Configuration saved to
/etc/frr/<daemon>.conf" line per daemon, or a single "Integrated
configuration saved to /etc/frr/frr.conf" line when the node keeps an
integrated config file, while a save into an unwritable /etc/frr prints
"Building Configuration..." followed by "Can't open configuration file
/etc/frr/zebra.conf.XXXXXX." per daemon and nothing else. configure()
therefore requires positive evidence of a save: any "Can't open configuration
file" line, or the absence of every "configuration saved to" line, is
reported as {"success": False, "error": <the offending save line>,
"save_output": <the write-memory output>}. The commands did reach the running
config in that case, they just are not persisted, which is why the failure
payload calls them "attempted" rather than "applied" (a rejected batch may
also have applied some prefix of its lines, so "applied" was wrong there
too).

IMPORTANT LIMITATION, stated plainly rather than papered over: this is
best-effort, the same as drivers/frr_l3. Not every rejected command prints a
"%" line (see drivers/frr_l3/driver.py's module docstring for a verified live
example with a malformed next hop), so {"success": True} here means "the
device reported nothing genuinely wrong", not "the configuration is
confirmed present". This driver deliberately does not read back
`show running-config` after a configure() call to close that gap: verifying
a driver's own work through its own read path is the anti-pattern the live
NOS-lab tests exist to avoid (tests/nos_lab/test_frr_mgmt_driver_live.py
verifies independently via a separate `docker exec ... vtysh` call instead).

Keep in sync with drivers/frr_l3/driver.py: the two packages share this
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
    values"). Keep in sync with drivers/frr_l3/driver.py.
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


def _find_error_lines(output):
    """Return every vtysh "%"-prefixed line in `output`, in order.

    Best-effort, not proof of success: see the module docstring's IMPORTANT
    LIMITATION note. A rejected command that prints no "%" line (verified
    live for a malformed FRR route next-hop; see drivers/frr_l3/driver.py's
    module docstring) contributes nothing here even though nothing was
    applied.

    Returns every match, not just the first: a caller that only inspected the
    first "%" line could be fooled by a benign line arriving before a genuine
    one in the same multi-command batch (see configure()'s docstring and the
    module docstring's classification section).

    Duplicated, not imported, from drivers/frr_l3/driver.py's helper of the
    same name: driver packages are uploaded and cached standalone
    (docs/DRIVERS.md, "Package structure"), so a cross-package import would
    not resolve in the execution sandbox. Keep the two in sync if the
    underlying "%" detection ever changes.
    """
    return [line.strip() for line in output.splitlines() if line.strip().startswith("%")]


# The one "%" line this driver currently knows to be benign: FRR's response to
# removing a route that is already absent. The route being removed is already
# gone, which is the desired end state regardless of the device's wording;
# see drivers/frr_l3/driver.py's own (independently verified) use of the same
# marker for its single-line remove_route. This set is deliberately NOT
# shared or inherited from frr_l3, or from any other driver: which of a
# device's complaints are benign is vendor- and command-specific, verified
# against THIS device, and every driver author has to work out their own
# (docs/DRIVERS.md, "A driver must report a device rejection as a failure").
#
# Matched with startswith, never with `in` (issue #779): FRR echoes the
# offending command inside "% Unknown command: <the command>", so a command
# that merely CONTAINS this phrase would otherwise have its genuine rejection
# classified as benign. See the module docstring's anchoring paragraph.
_ALREADY_ABSENT_MARKER = "% Refusing to remove a non-existent route"

# Save ("write memory") classification, verified live 2026-09-12; see the
# module docstring's persistence paragraph. The OK marker is matched
# case-insensitively because vtysh renders the per-daemon form as
# "Configuration saved to ..." and the integrated form as "Integrated
# configuration saved to ...".
_SAVE_OK_MARKER = "configuration saved to"
_SAVE_FAILURE_MARKER = "can't open configuration file"


def _classify_error_lines(error_lines):
    """Partition `error_lines` into (genuine, benign) in their original order.

    "Benign" here means only the one anchored marker above; everything else
    is genuine. Both lists preserve the order the lines appeared in the
    device output, so a caller that reports "the first genuine failure" is
    reporting the first one that actually matters, not merely the first "%"
    line.
    """
    genuine = [line for line in error_lines if not line.startswith(_ALREADY_ABSENT_MARKER)]
    benign = [line for line in error_lines if line.startswith(_ALREADY_ABSENT_MARKER)]
    return genuine, benign


def _save_failure_line(save_output):
    """Return the line proving a failed "write memory", or None if it saved.

    Positive evidence is required, not merely the absence of a complaint: a
    failed save prints no "%" line and exits 0 (module docstring), so
    "nothing looked wrong" is not usable as success here. Two rules, either
    of which reports failure:

      - any "Can't open configuration file ..." line, which is what vtysh
        prints per daemon when it cannot write the file. Reported verbatim,
        first one only, since that is the operator-facing text.
      - no "configuration saved to ..." line anywhere in the output, which
        covers a future wording this driver does not know: with no evidence
        the config was written, the safe classification is failure.
    """
    lines = [line.strip() for line in (save_output or "").splitlines() if line.strip()]
    for line in lines:
        if _SAVE_FAILURE_MARKER in line.lower():
            return line
    if any(_SAVE_OK_MARKER in line.lower() for line in lines):
        return None
    return "write memory reported no saved configuration file"


class Driver:
    """FRRouting router driver, SSH-to-vtysh via netmiko."""

    def __init__(self, context):
        """Record the connection context. Deliberately never raises.

        Nothing here parses or validates: a constructor that raises for bad
        field_data makes status() a hard sandbox failure instead of
        {"reachable": False} (issue #780). HERD_port is parsed in _connect(),
        and the missing-host/login check lives in login().
        """
        self.context = context
        self.dry_run = bool(context.get("dry_run", False))
        self.host = context.get("HERD_ip") or context.get("HERD_ip_address")
        self.username = context.get("HERD_login") or context.get("HERD_username")
        self.password = context.get("HERD_password")
        self.port_raw = context.get("HERD_port")
        self._conn = None

    # --- published config schema --------------------------------------------

    @classmethod
    def config_schema(cls):
        """Schema for the `configure` action's kwargs.

        The platform's neutral Management vocabulary ({vlan, ip, hostname,
        description}) cannot express raw routing config, so this driver publishes
        its own schema: a list of vtysh config-mode lines. The execution service
        prefers this over the registry schema when validating a `configure` call
        (see issue #23), so {commands: [...]} is accepted instead of rejected as
        an additional property.

        Kept draft-2020-12 safe on purpose: no $ref, $id, or $schema, so the
        execution-side sanitizer accepts it without stripping anything.
        """
        return {
            "type": "object",
            "properties": {
                "commands": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1, "maxLength": 512},
                    "minItems": 1,
                    "maxItems": 100,
                    "description": (
                        "vtysh config-mode lines, e.g. 'ip route 192.0.2.0/24 blackhole'."
                    ),
                },
                "command": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 512,
                    "description": "A single vtysh config line (convenience form).",
                },
            },
            "additionalProperties": False,
            "minProperties": 1,
        }

    # --- connection helpers -------------------------------------------------

    def _connect(self):
        """Open a netmiko session to vtysh. Never called in dry-run.

        HERD_port is parsed here, not in __init__, so a bad port value raises
        DriverError from the call that needed the connection and status()
        still degrades to {"reachable": False} (issue #780).
        """
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

    # --- Management contract ------------------------------------------------

    def login(self):
        """Open the session (no-op in dry-run; the vtysh login is implicit)."""
        if self.dry_run:
            record_command(
                f"ssh {self.username}@{self.host}",
                response="(simulated)",
                exit_status="simulated",
            )
            return {"success": True, "simulated": True}
        if not self.host or not self.username:
            raise DriverError("missing HERD_ip / HERD_login for the device")
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

    def configure(self, **cfg):
        """Apply config lines to the router and persist them.

        Accepts `commands`: a list of vtysh config-mode lines (e.g.
        ["ip route 192.0.2.0/24 blackhole"]). Also accepts a single `command`
        string for convenience. netmiko's send_config_set wraps them in
        configure terminal / end.

        Two independent things are classified, both documented at length in
        the module docstring:

        1. The config output. EVERY "%" line is collected and partitioned
           into genuine and benign (currently just the anchored "already
           absent" removal marker). Any genuine line reports
           {"success": False, "error": <the first genuine line>, "output":
           <full output>, "attempted": <the commands>}; if every "%" line
           found is benign, this is still a success, with the benign lines
           surfaced under "benign_warnings" rather than "error".

        2. The "write memory" output, which is where a config that applied
           to the running config but could not be persisted shows up (no "%"
           line, exit 0). A save that cannot be proven to have happened
           reports {"success": False, "error": <the offending save line>,
           "output": <the config output>, "save_output": <the write-memory
           output>, "attempted": <the commands>}, plus "benign_warnings" if
           any were seen.

        The failure payloads say "attempted", not "applied": neither of those
        cases is a batch that landed in full, and the success payload is the
        only one that keeps "applied".
        """
        commands = cfg.get("commands")
        if commands is None and "command" in cfg:
            commands = [cfg["command"]]
        if not commands:
            raise DriverError("configure requires `commands` (a list) or `command` (a string)")
        if isinstance(commands, str):
            commands = [commands]

        if self.dry_run:
            for line in commands:
                record_command(line, response="(simulated)", exit_status="simulated")
            return {"success": True, "simulated": True, "applied": list(commands)}

        conn = self._connect()
        output = conn.send_config_set(commands)
        error_lines = _find_error_lines(output)
        genuine, benign = _classify_error_lines(error_lines)
        if genuine:
            record_command("\n".join(commands), response=output, exit_status="error")
            return {
                "success": False,
                "error": genuine[0],
                "output": output,
                "attempted": list(commands),
            }
        record_command("\n".join(commands), response=output)
        # Persist to startup config so the change survives a daemon restart.
        save_output = conn.save_config()
        save_failure = _save_failure_line(save_output)
        if save_failure is not None:
            record_command("write memory", response=save_output, exit_status="error")
            result = {
                "success": False,
                "error": save_failure,
                "output": output,
                "save_output": save_output,
                "attempted": list(commands),
            }
            if benign:
                result["benign_warnings"] = benign
            return result
        record_command("write memory", response=save_output)
        result = {"success": True, "applied": list(commands), "output": output}
        if benign:
            result["benign_warnings"] = benign
        return result

    def backup(self):
        """Return the running configuration.

        See the module docstring's "Error detection" section: a vtysh "%"
        line in the output means the `show running-config` command itself was
        rejected (e.g. a broken or wedged session), not that the device has
        an empty config, and is reported as {"success": False}, never a
        bogus {"success": True, "config": "% ..."}. backup() has no known
        benign "%" case (it is a read, not an operation with a desired end
        state to converge on), so any "%" line found is treated as genuine.
        """
        if self.dry_run:
            record_command("show running-config", response="(simulated)", exit_status="simulated")
            return {"success": True, "simulated": True, "config": None}
        conn = self._connect()
        running = conn.send_command("show running-config")
        error_lines = _find_error_lines(running)
        if error_lines:
            record_command("show running-config", response=running, exit_status="error")
            return {"success": False, "error": error_lines[0], "output": running}
        record_command("show running-config", response=running)
        return {"success": True, "config": running}

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
            return {"reachable": reachable, "version": version.splitlines()[0] if version else ""}
        except Exception as e:  # noqa: BLE001 - status must not raise; report unreachable
            record_command("show version", response=str(e), exit_status="error")
            return {"reachable": False, "error": str(e)}
