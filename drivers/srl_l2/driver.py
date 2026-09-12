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
prior command silently changes for the rest of that SSH session. A command
like `network-instance vlan100 interface ethernet-1/1.100` sets no trailing
scalar value, so the CLI treats it as "enter that list entry" and navigates
the session into it; an _apply batch goes down one session in one
send_config_set, so the NEXT command in that batch is then parsed relative
to the stale context instead of the root, and a perfectly-correct-looking
command such as `network-instance vlan101 type mac-vrf` fails with a
"Parsing error: Unknown token" it would never hit issued alone. This was
found empirically against the checked-in lab, not merely inferred from the
docs. A leading `/` roots every command and leaves the session back at the
top-level context afterward regardless of what the command itself did.

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
text itself.

CANDIDATE DISCIPLINE (issue #778). SR Linux stages every `set` and `delete`
in a per-user PRIVATE CANDIDATE, and only `commit stay` applies it. Two
facts about that candidate, both reproduced live against this exact node,
dictate the ordering inside _apply:

  - netmiko does NOT stop sending at the first rejected line, and a commit
    then applies the valid PREFIX of the batch. Proven live: a two-line
    batch whose second line is a "Parsing error:" still staged its first
    line, and the following `commit stay` answered "All changes have been
    committed." So a commit issued before the SET output is judged silently
    half-applies a batch HERD is about to record as failed. _apply
    therefore judges the set output and returns failure BEFORE it ever
    calls commit().
  - the private candidate PERSISTS ACROSS SSH SESSIONS. Proven live: stage
    a line in one session and quit without committing, and the next
    session's `enter candidate private` shows it under `diff` and its
    `commit stay` applies it. The execution sandbox runs ONE PROCESS PER
    ACTION (services/execution/app/services/driver_sandbox.py), so login,
    each mutating call, and logout are separate processes with separate SSH
    sessions: there is no shared session, but a candidate left dirty by a
    call that died (an exception, a refused commit, the sandbox rlimit
    killing the process) still rides into the NEXT call's commit through
    the DEVICE. _apply therefore runs `discard stay` at ENTRY, before it
    stages anything, and again on every failure and exception path. The
    entry discard needs candidate mode first: `discard stay` issued in
    running mode is itself a "Parsing error:" (verified live), so
    _discard_candidate enters candidate mode before discarding.

IMPORTANT LIMITATION, stated plainly rather than papered over: all of the
above is best-effort. The rejection scan only sees what the device chooses
to print, and "Nothing to commit." proves nothing on its own, so
{"success": True} means "the device reported no error", not "the
configuration is proven present in the running datastore". Nothing in this
driver parses `info from running` to close that gap, and nothing should:
verifying a driver's own work through the driver's own read path is the
exact anti-pattern the live NOS-lab tests exist to avoid
(tests/nos_lab/test_srl_l2_driver_live.py verifies independently through a
separate `docker exec nos-test-srl sr_cli` session). The discard is
best-effort in the same way: it cannot clean a candidate on a node that has
become unreachable, and a SIGKILL landing between the staging and the
discard still leaves one behind. That residue is precisely what the NEXT
call's entry discard exists to absorb.

Connection params come from the device field_data as HERD_-prefixed context
keys (mirrors drivers/frr_mgmt/driver.py):
  HERD_ip       host/IP the execution service can reach (e.g. 127.0.0.1)
  HERD_login    SSH username
  HERD_password SSH password
Optional: HERD_port (blank or missing means 22; see _parse_port for why a
non-integer raises from _connect rather than from __init__).

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


def _parse_port(value):
    """Resolve the optional HERD_port context value to an int (issue #780).

    A device template's optional port field reaches the driver context
    verbatim (execution_service.py builds the context from field_data), so a
    field the operator left alone arrives as "" rather than absent, and
    int("") raises. Blank or missing therefore means the SSH default, 22.

    A genuinely non-numeric value is a real misconfiguration and does raise,
    but the raise belongs HERE, on the connect path, not in __init__: the
    contract in docs/DRIVERS.md is that status() never raises, and health
    polling needs {"reachable": False} rather than a hard sandbox failure
    from a constructor that blew up before any method ran. Mutating calls
    still surface the clear DriverError message.
    """
    if value is None:
        return 22
    if isinstance(value, str) and not value.strip():
        return 22
    try:
        return int(value)
    except (TypeError, ValueError):
        raise DriverError("HERD_port must be an integer") from None


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
        # Raw on purpose: parsing (and any refusal) happens in _connect. See _parse_port.
        self.port = context.get("HERD_port", 22)
        self._conn = None

    # --- connection helpers -------------------------------------------------

    def _connect(self):
        """Open a netmiko session to the SR Linux CLI. Never called in dry-run.

        HERD_port is parsed here rather than in __init__ so a bad port value
        degrades the way every other connection failure does: status()
        catches it and reports {"reachable": False}, mutating calls get the
        DriverError message. See _parse_port (issue #780).
        """
        port = _parse_port(self.port)
        from netmiko import ConnectHandler

        if self._conn is None:
            self._conn = ConnectHandler(
                device_type="nokia_srl",
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

    def _discard_candidate(self, conn):
        """Best-effort `discard stay`, leaving the private candidate empty.

        Enters candidate mode first: netmiko's send_config_set for this
        platform deliberately does NOT exit config mode, but a session that
        has not staged anything yet is still in running mode, where `discard
        stay` is itself a "Parsing error:" (verified live). config_mode() is
        a no-op when the session is already in candidate mode.

        Swallows everything. This runs on cleanup paths where a failure is
        already being reported (or an exception is already propagating), and
        a cleanup error must not replace the real one. The cost of a
        swallowed failure here is bounded: the NEXT call's entry discard
        clears whatever this one could not.
        """
        try:
            conn.config_mode()
            return conn._discard()
        except Exception:  # noqa: BLE001 - best-effort cleanup; the real outcome still stands
            return None

    def _apply(self, commands):
        """Send an absolute-path command batch in candidate mode and commit it.

        Real path only (never called in dry-run). commit() is mandatory,
        never optional: a candidate change that is not committed is
        silently not applied.

        Ordering is the whole point of this method (issue #778); see the
        module docstring's CANDIDATE DISCIPLINE section for the live
        evidence behind each step:

          1. discard at ENTRY, so a candidate left dirty by an earlier call
             that died cannot ride into this call's commit. The candidate is
             per-user and persists across SSH sessions, so a fresh sandbox
             process is no protection.
          2. send the batch, then judge the SET output and return failure
             BEFORE commit(). netmiko keeps sending past a rejected line, so
             a commit here would apply the batch's valid prefix while the
             caller is told the whole thing failed.
          3. judge the COMMIT output, discarding on refusal.
          4. discard on any exception path before it propagates.

        Returns {"success": True} on a clean apply (including a legitimate
        idempotent no-op), or {"success": False, "error": ...} when the SET
        or the COMMIT output carried a genuine device rejection (see
        _rejection_error). Failure means nothing from this batch was
        committed by this call.
        """
        conn = self._connect()
        candidate_handled = False
        try:
            self._discard_candidate(conn)

            output = conn.send_config_set(commands)
            set_error = _rejection_error(output)
            record_command(
                "\n".join(commands), response=output, exit_status="error" if set_error else "ok"
            )
            if set_error:
                self._discard_candidate(conn)
                candidate_handled = True
                return {"success": False, "error": set_error, "output": output}

            commit_output = conn.commit()
            commit_error = _rejection_error(commit_output)
            record_command(
                "commit stay",
                response=commit_output,
                exit_status="error" if commit_error else "ok",
            )
            if commit_error:
                self._discard_candidate(conn)
                candidate_handled = True
                return {"success": False, "error": commit_error, "output": commit_output}

            # A clean commit leaves the candidate empty by construction.
            candidate_handled = True
            return {"success": True}
        finally:
            if not candidate_handled:
                self._discard_candidate(conn)

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
