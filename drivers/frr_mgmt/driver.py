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
Optional: HERD_port (default 22).

dry_run is honored on every mutating method: when set, the commands are recorded
via record_command(... exit_status="simulated") and NO SSH connection is opened,
so a dry run never touches the wire. driver_metadata.json advertises
supports_dry_run: true, which is binding (see docs/DRIVERS.md).
"""

try:
    from driver_transcript import record_command
except ImportError:  # running outside the execution sandbox (e.g. unit tests)

    def record_command(*args, **kwargs):
        pass


class DriverError(Exception):
    """Raised when a real (non-dry-run) operation against the device fails."""


class Driver:
    """FRRouting router driver, SSH-to-vtysh via netmiko."""

    def __init__(self, context):
        self.context = context
        self.dry_run = bool(context.get("dry_run", False))
        self.host = context.get("HERD_ip") or context.get("HERD_ip_address")
        self.username = context.get("HERD_login") or context.get("HERD_username")
        self.password = context.get("HERD_password")
        self.port = int(context.get("HERD_port", 22))
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
        """Open a netmiko session to vtysh. Never called in dry-run."""
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
        """Apply config lines to the router.

        Accepts `commands`: a list of vtysh config-mode lines (e.g.
        ["ip route 192.0.2.0/24 blackhole"]). Also accepts a single `command`
        string for convenience. netmiko's send_config_set wraps them in
        configure terminal / end.
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
        record_command("\n".join(commands), response=output)
        # Persist to startup config so the change survives a daemon restart.
        save_output = conn.save_config()
        record_command("write memory", response=save_output)
        return {"success": True, "applied": list(commands), "output": output}

    def backup(self):
        """Return the running configuration."""
        if self.dry_run:
            record_command("show running-config", response="(simulated)", exit_status="simulated")
            return {"success": True, "simulated": True, "config": None}
        conn = self._connect()
        running = conn.send_command("show running-config")
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
