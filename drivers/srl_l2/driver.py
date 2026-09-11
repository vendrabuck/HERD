"""Nokia SR Linux Layer 2 Switch driver: VLAN (mac-vrf bridge domain) management
over SSH via netmiko's nokia_srl platform.

Connection type: Layer 2 Switch (login, logout, create_vlan, add_to_vlan,
remove_from_vlan, delete_vlan, status).

SR Linux has no bare "VLAN" object the way Cisco IOS does (a single global
`vlan 100` a trunk or access port references directly). Each VLAN is instead
modeled as two separate pieces:

  - a network-instance of type mac-vrf, named deterministically from the
    vlan id as "vlan<id>" (create_vlan / delete_vlan). The name is otherwise
    arbitrary; "vlan<id>" was chosen for readability in `sr_cli` output.
  - per member port, a tagged-or-untagged bridged subinterface on that port
    (index = the vlan id itself, so the subinterface is "<port>.<vlan_id>"),
    bound into the network-instance (add_to_vlan / remove_from_vlan).

See docs/DRIVERS.md ("SR Linux Layer 2 driver (drivers/srl_l2)") for the
contract mapping and docs/NOS_LAB.md for the live lab this was verified
against (Nokia's `[FACTORY]` config-mode trap in particular).

Every mutating command is issued as an ABSOLUTE path, `set /...` or
`delete /...`, never a bare relative one. This is load-bearing, not
cosmetic: SR Linux's candidate-mode CLI keeps a "current context" that a
prior command can silently change for the rest of the session. A command
like `network-instance vlan100 interface ethernet-1/1.100` sets no trailing
scalar value, so the CLI treats it as "enter that list entry" and navigates
the session into it; since multiple mutating calls share one login/logout
session (see docs/DRIVERS.md, "When methods are called"), the NEXT command
in that same session is then parsed relative to the stale context instead of
the root, and a perfectly-correct-looking command such as
`network-instance vlan101 type mac-vrf` fails with a "Parsing error: Unknown
token" it would never hit issued alone. This was found empirically against
the checked-in lab, not merely inferred from the docs. A leading `/` roots
every command and leaves the session back at the top-level context
afterward regardless of what the command itself did.

create_vlan and delete_vlan are idempotent by construction, not by any
special-casing in this driver: SR Linux's own candidate/commit model treats
redefining an already-identical network-instance, or deleting a path that
does not exist, as a no-op ("Nothing to commit."). This was verified live
against the lab, not assumed; see tests/nos_lab/test_srl_l2_driver_live.py.

Config is transactional: netmiko's nokia_srl platform enters candidate mode
automatically on send_config_set, and this driver explicitly calls
commit() ("commit stay") after every mutating batch. A candidate change
that is never committed is silently never applied, so every create_vlan,
add_to_vlan, remove_from_vlan, and delete_vlan call ends in a commit.

A rejected command is NOT an exception, it is a returned {"success": False}:
HERD keys provisioning success on the driver's returned payload
(execution_service.py's driver_result_failed helper), so a false "success"
here would make HERD record an ACTIVE VLAN membership the switch never
actually accepted. Every mutating method therefore scans both the `set`
and the `commit` output for a genuine device rejection ("Parsing error:",
"Invalid value", or a line starting with "Error:", all verified live
against this exact node) before reporting success. This also disambiguates
create_vlan/delete_vlan idempotency from a false success: SR Linux's
"Nothing to commit." is what BOTH a legitimately idempotent no-op AND a
`set` that was rejected outright (and so never staged) produce, since a
rejected command never enters the candidate for commit to act on; the
error-marker scan is what tells them apart, not the "Nothing to commit."
text itself. A detected rejection also discards the candidate
(conn._discard(), netmiko's private-candidate-clear command for this
platform) before returning failure: a partially-staged batch (some lines
valid, one rejected) can leave the candidate dirty even though nothing
committed, and since mutating calls share one login/logout session, that
leftover diff would otherwise silently ride along into the NEXT call's
commit on this session (verified live: a batch with one out-of-range field
staged its three sibling lines into the candidate, then failed at commit
with a semantic "inconsistent" error, leaving the candidate dirty until
discarded).

Connection params come from the device field_data as HERD_-prefixed context
keys (mirrors drivers/frr_mgmt/driver.py):
  HERD_ip       host/IP the execution service can reach (e.g. 127.0.0.1)
  HERD_login    SSH username
  HERD_password SSH password
Optional: HERD_port (default 22).

dry_run is honored on every mutating method: when set, the commands that
would be sent are recorded via record_command(... exit_status="simulated")
and no SSH connection is opened. driver_metadata.json advertises
supports_dry_run: true, which is binding (see docs/DRIVERS.md).
"""

try:
    from driver_transcript import record_command
except ImportError:  # running outside the execution sandbox (e.g. unit tests)

    def record_command(*args, **kwargs):
        pass


class DriverError(Exception):
    """Raised when a real (non-dry-run) operation is missing what it needs."""


def _vlan_name(vlan_id):
    """Deterministic network-instance name for a VLAN id: "vlan<id>"."""
    return f"vlan{vlan_id}"


def _create_vlan_commands(vlan_id):
    return [f"set / network-instance {_vlan_name(vlan_id)} type mac-vrf"]


def _delete_vlan_commands(vlan_id):
    return [f"delete / network-instance {_vlan_name(vlan_id)}"]


def _add_to_vlan_commands(port, vlan_id, tag):
    """Build the tagged or untagged bridged-subinterface-plus-binding commands.

    tag="untagged" gets the bare `vlan encap untagged` form; anything else
    (including the default "tagged") gets the single-tagged encap with the
    vlan id, matching docs/DRIVERS.md's two documented forms.
    """
    if tag == "untagged":
        encap = "vlan encap untagged"
    else:
        encap = f"vlan encap single-tagged vlan-id {vlan_id}"
    name = _vlan_name(vlan_id)
    return [
        f"set / interface {port} vlan-tagging true",
        f"set / interface {port} subinterface {vlan_id} type bridged",
        f"set / interface {port} subinterface {vlan_id} {encap}",
        f"set / network-instance {name} interface {port}.{vlan_id}",
    ]


def _remove_from_vlan_commands(port, vlan_id):
    """The delete form of add_to_vlan's subinterface and binding.

    Deliberately does NOT touch `vlan-tagging` on the port: that flag is not
    tied to any one vlan_id, and the port may still carry other VLANs on
    other subinterfaces. Turning it off here would be a scope violation of
    "remove this one membership", not its mirror.
    """
    name = _vlan_name(vlan_id)
    return [
        f"delete / network-instance {name} interface {port}.{vlan_id}",
        f"delete / interface {port} subinterface {vlan_id}",
    ]


_ERROR_SUBSTRINGS = ("Parsing error:", "Invalid value")


def _rejection_error(output):
    """Return the offending text if `output` carries a genuine device
    rejection, else None.

    SR Linux's own commit response is ambiguous by itself: "Nothing to
    commit." is what BOTH a legitimately idempotent no-op call AND a `set`
    that was rejected outright produce, since a rejected command never
    enters the candidate for commit to act on. The disambiguator is
    whether the SET or the COMMIT output itself carried an error marker.
    A syntax-level rejection surfaces as "Parsing error:" or "Invalid
    value" somewhere in the `send_config_set` output; a config that
    parses but is refused for semantic reasons can instead fail only at
    commit time, surfacing as a line starting with "Error:" (e.g. "Error:
    Commit failed"). All three were reproduced live against this exact
    node before being pinned here.

    Returns the offending LINE, not the whole transcript: this value is what
    HERD stores in a wiring assignment's last_error column, so it has to stay
    readable there. The full device output rides alongside it under "output".
    This matches drivers/frr_l3, whose "error" is likewise a single line.
    """
    if not output:
        return None
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("Error:") or any(m in stripped for m in _ERROR_SUBSTRINGS):
            return stripped
    return None


class Driver:
    """Nokia SR Linux Layer 2 switch driver, SSH via netmiko's nokia_srl platform."""

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
        """Open a netmiko session to the SR Linux CLI. Never called in dry-run."""
        from netmiko import ConnectHandler

        if self._conn is None:
            self._conn = ConnectHandler(
                device_type="nokia_srl",
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

    def _apply(self, commands):
        """Send an absolute-path command batch in candidate mode and commit it.

        Real path only (never called in dry-run). commit() is mandatory,
        never optional: a candidate change that is not committed is
        silently not applied.

        Returns {"success": True} on a clean apply (including a
        legitimate idempotent no-op), or {"success": False, "error": ...}
        if the SET or the COMMIT output carried a genuine device
        rejection (see _rejection_error). A detected rejection also
        discards the candidate so a partially-staged, uncommitted batch
        never rides along into a later call's commit on this shared
        session.
        """
        conn = self._connect()
        output = conn.send_config_set(commands)
        set_error = _rejection_error(output)
        record_command(
            "\n".join(commands), response=output, exit_status="error" if set_error else "ok"
        )

        commit_output = conn.commit()
        commit_error = _rejection_error(commit_output)
        record_command(
            "commit stay", response=commit_output, exit_status="error" if commit_error else "ok"
        )

        error = set_error or commit_error
        if error:
            try:
                conn._discard()
            except Exception:  # noqa: BLE001 - best-effort cleanup; the failure below still stands
                pass
            return {
                "success": False,
                "error": error,
                "output": output if set_error else commit_output,
            }
        return {"success": True}

    def _record_simulated(self, commands):
        for line in commands:
            record_command(line, response="(simulated)", exit_status="simulated")

    # --- Layer 2 contract -----------------------------------------------------

    def login(self):
        """Open the session (no-op in dry-run)."""
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
        record_command(f"ssh {self.username}@{self.host}", response="(connected)")
        return {"success": True}

    def logout(self):
        if self.dry_run:
            record_command("exit", response="(simulated)", exit_status="simulated")
            return {"success": True, "simulated": True}
        self._disconnect()
        record_command("exit", response="(disconnected)")
        return {"success": True}

    def create_vlan(self, vlan_id):
        """Define a mac-vrf network-instance for vlan_id.

        MUST be idempotent, and is: SR Linux treats redefining an
        identical network-instance as a no-op commit ("Nothing to
        commit."), so this method never needs to check for existence
        first.
        """
        commands = _create_vlan_commands(vlan_id)
        if self.dry_run:
            self._record_simulated(commands)
            return {"success": True, "simulated": True}
        return self._apply(commands)

    def add_to_vlan(self, port, vlan_id, tag="tagged"):
        commands = _add_to_vlan_commands(port, vlan_id, tag)
        if self.dry_run:
            self._record_simulated(commands)
            return {"success": True, "simulated": True}
        return self._apply(commands)

    def remove_from_vlan(self, port, vlan_id):
        commands = _remove_from_vlan_commands(port, vlan_id)
        if self.dry_run:
            self._record_simulated(commands)
            return {"success": True, "simulated": True}
        return self._apply(commands)

    def delete_vlan(self, vlan_id):
        """Delete vlan_id's network-instance.

        Deleting a network-instance that does not exist also succeeds (the
        idempotency mirror of create_vlan): SR Linux treats it as a no-op
        commit too, verified the same way.
        """
        commands = _delete_vlan_commands(vlan_id)
        if self.dry_run:
            self._record_simulated(commands)
            return {"success": True, "simulated": True}
        return self._apply(commands)

    def status(self):
        """Reachability check: open a session and read the version banner."""
        if self.dry_run:
            record_command("show version", response="(simulated)", exit_status="simulated")
            return {"reachable": True, "simulated": True}
        try:
            conn = self._connect()
            version = conn.send_command("show version")
            record_command("show version", response=version)
            return {"reachable": "SR Linux" in version}
        except Exception as e:  # noqa: BLE001 - status must not raise; report unreachable
            record_command("show version", response=str(e), exit_status="error")
            return {"reachable": False, "error": str(e)}
