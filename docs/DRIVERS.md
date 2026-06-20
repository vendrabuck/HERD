# Driver Developer Guide

This document defines the contract between user-authored driver packages and the HERD
execution service. Drivers are Python packages that the execution service calls at
specific lifecycle events (reservations, device health checks) to interact with
infrastructure devices.

## Connection types and their driver contracts

Each device template in HERD references a driver package and declares a connection type.
The connection type determines which methods the execution service will call on the
driver.

| Connection type | Purpose | Required methods |
|---|---|---|
| Layer 1 Switch | Physical port cross-connect | login, logout, connect_ports, disconnect_ports, status |
| Layer 2 Switch | VLAN management | login, logout, create_vlan, add_to_vlan, remove_from_vlan, delete_vlan, status |
| Layer 3 Switch | Routing management (future) | login, logout, configure_route, remove_route, status |
| Management | DUT session management | login, logout, configure, backup, status |

Layer 1 Switch, Layer 2 Switch, and Management contracts are implemented.
Layer 3 Switch is documented here as a planned interface.

---

## Package structure

Driver packages are uploaded as `.zip` or `.tar.gz` archives (max 10 MB). The archive
must contain the following at its root:

```
driver.py             # REQUIRED: must contain a class named Driver
driver_metadata.json  # OPTIONAL: capability declarations (see "Dry-run support" below)
requirements.txt      # OPTIONAL: pip dependencies installed before execution
lib/                  # OPTIONAL: supporting Python modules importable by driver.py
```

The execution service extracts the archive, optionally installs dependencies from
`requirements.txt`, then loads `driver.py` and instantiates the `Driver` class.

---

## Dry-run support

Apply jobs and direct execute requests may set `dry_run=true`. When set, the driver
is expected to record the commands it WOULD have sent via the per-command transcript
helper but skip the actual wire I/O. The captured transcript is what a user reviews
before confirming a real apply through the AI assistant flow.

To opt in, ship `driver_metadata.json` at the package root:

```json
{
  "supports_dry_run": true,
  "version": "1.0",
  "vendor": "ExampleVendor",
  "notes": "All mutating methods honor context['dry_run']."
}
```

Drivers without this file (or with `supports_dry_run: false`) cannot be scheduled
in dry-run mode: the inventory schedule endpoint rejects the request with HTTP 422,
and the execution sandbox refuses to spawn the subprocess as a second-line defense.

In your `Driver` class, read the flag from the context dict and gate every mutating
method on it (`context.get("dry_run", False)`):

```python
from driver_transcript import record_command


class Driver:
    def __init__(self, context):
        self.context = context
        self.dry_run = bool(context.get("dry_run", False))

    def configure(self, **cfg):
        for key, value in cfg.items():
            command = f"set {key} {value}"
            if self.dry_run:
                record_command(command, response="(simulated)", exit_status="simulated")
            else:
                response = self._transport.send(command)
                record_command(command, response=response)
        return {"success": True, "simulated": self.dry_run}
```

The `supports_dry_run` claim is binding: a driver that advertises support but still
hits the wire on a dry-run request will silently push configuration changes that a
user thought they were only simulating. Treat the flag with the same care as any
other safety contract. Failing to record commands when dry-run is requested is also
a problem, because the user-facing confirmation modal will appear empty and a real
apply may follow against unverified intent.

---

## Golden-transcript regression tests

A small mock-Cisco-IOS fixture driver lives at
`services/execution/tests/fixtures/drivers/mock_ios/`. It implements the L2
Switch contract against an in-process recorder rather than a real socket, so
its `record_command` output captures realistic CLI strings without needing
hardware. Each `(driver, action, kwargs, dry_run)` tuple is pinned in a JSON
fixture under `services/execution/tests/golden_transcripts/`, and the
parametrized `test_golden_transcripts.py` suite asserts the captured
transcript matches its fixture exactly.

If the mock driver's CLI translation drifts, the suite fails with a diff
naming the regenerator command. If the change is intentional (e.g. you
updated the driver to emit a new syntax for some reason), regenerate:

```bash
cd services/execution
uv run python tests/regenerate_golden_transcripts.py
```

The script overwrites the JSON fixtures in place; review the diff in your
VCS before committing. Running blindly will mask real regressions.

Adding a new case is one entry in the `CASES` list at the top of
`regenerate_golden_transcripts.py`; the fixture filename derives from the
`id` field. Add a new fixture driver under `tests/fixtures/drivers/` if you
need to cover a different vendor or connection type.

This is the "tested without hardware" story for iter 3: golden fixtures
prove driver CLI output is stable, not that it works against a real device.
A driver that ships with green goldens AND green real-hardware acceptance
testing (done separately, outside this suite) is fully covered.

---

## Layer 1 Switch driver contract

Layer 1 switches make physical (electrical or optical) connections between two ports
within their chassis. The execution service calls L1 drivers when reservations are
created or ended, connecting or disconnecting the switch ports that link DUT devices.

### Class definition

```python
class Driver:
    """Layer 1 Switch driver.

    The execution service instantiates this class once per operation batch,
    passing all device parameters and execution metadata in the context dict.
    """

    def __init__(self, context: dict):
        """Store context for use in subsequent method calls.

        Args:
            context: flat dict containing all HERD-injected parameters
                     (prefixed with HERD_) and any user-defined variables.
                     See "Context reference" below for the full key list.
        """
        self.context = context

    def login(self) -> dict:
        """Establish a session with the switch.

        Called before connect_ports, disconnect_ports, or status. Use the
        HERD_ip_address, HERD_username, HERD_password (or whichever fields
        were defined in the device template) to authenticate.

        Returns:
            dict with at minimum {"success": bool}. Include "message" for
            details on failure.
        """
        ...

    def logout(self) -> dict:
        """Tear down the session with the switch.

        Called after the operation batch completes (after connect_ports,
        disconnect_ports, or status).

        Returns:
            dict with at minimum {"success": bool}.
        """
        ...

    def connect_ports(self, port_a: str, port_b: str) -> dict:
        """Make a physical connection between two ports on the switch.

        Args:
            port_a: name of the first switch port (e.g. "1/1/1")
            port_b: name of the second switch port (e.g. "1/1/2")

        Returns:
            dict with at minimum {"success": bool}. Include "message" for
            details on failure.
        """
        ...

    def disconnect_ports(self, port_a: str, port_b: str) -> dict:
        """Remove a physical connection between two ports on the switch.

        Args:
            port_a: name of the first switch port (e.g. "1/1/1")
            port_b: name of the second switch port (e.g. "1/1/2")

        Returns:
            dict with at minimum {"success": bool}. Include "message" for
            details on failure.
        """
        ...

    def status(self) -> dict:
        """Health check: verify the switch is reachable and operational.

        Called when a device is first added to HERD or on admin-triggered
        checks. No ports are involved.

        Returns:
            dict with at minimum {"reachable": bool}. Include additional
            diagnostic info as needed (firmware version, uptime, etc.).
        """
        ...
```

### When methods are called

| Event | Sequence |
|---|---|
| Reservation created (DUTs connected through L1 switch) | login(), connect_ports(a, b) for each port pair, logout() |
| Reservation cancelled or completed | login(), disconnect_ports(a, b) for each port pair, logout() |
| Device added to HERD or admin health check | login(), status(), logout() |

When multiple port pairs go through the same L1 switch in a single reservation, the
execution service batches them: one login/logout wrapping all connect_ports or
disconnect_ports calls.

### Port identity

The port_a and port_b arguments are the L1 switch's own port names (as defined in the
HERD inventory), not the DUT port names. The cabling topology in HERD maps DUT ports to
switch ports; the execution service resolves this mapping and passes only the switch-side
port names to the driver.

---

## Context reference

The execution service builds a flat `dict` and passes it to `Driver.__init__()`. All
HERD-injected keys are prefixed with `HERD_` to distinguish them from user-defined
variables in the driver code.

### Device field data

Every field defined in the device's template sections is included with the `HERD_`
prefix. For example, if the device template defines fields `ip_address`, `username`,
and `password`, the context will contain:

```python
{
    "HERD_ip_address": "10.0.1.50",
    "HERD_username": "admin",
    "HERD_password": "secretpass123",
    ...
}
```

Field values match the types defined in the template: strings for string/password/dropdown
fields, numbers for number fields, booleans for boolean fields.

### Execution metadata

These keys are always present regardless of the device template:

| Key | Type | Description |
|---|---|---|
| HERD_device_id | str (UUID) | The infrastructure device being driven |
| HERD_device_name | str | Name of the infrastructure device |
| HERD_connection_type | str | "Layer 1 Switch", "Layer 2 Switch", "Layer 3 Switch", or "Management" |
| HERD_reservation_id | str (UUID) or None | The reservation that triggered execution; None for status checks |
| HERD_user_id | str (UUID) | The user who initiated the action |

### Variable naming

- The `HERD_` prefix is reserved. Do not create variables starting with `HERD_` in your
  driver code; they may collide with system-injected values.
- You are free to define any other variables in your driver code without restrictions.

---

## Security

### Password handling

Device template fields of type "password" (e.g. switch credentials) are passed to the
driver in cleartext so the driver can authenticate with the device. These values are:

- Passed to the subprocess via a temporary file on disk (not command-line arguments),
  deleted immediately after the driver reads them
- Redacted as `***REDACTED***` in all execution logs and stored execution run records
- Never exposed in API responses beyond the execution service

### Execution sandbox

Driver code runs in a separate subprocess with these limits enforced:

- Timeout: 30 seconds for connect/disconnect, 10 seconds for status (configurable via
  `EXECUTION_TIMEOUT_SECONDS` and `STATUS_CHECK_TIMEOUT_SECONDS`).
- POSIX resource limits applied to the child via `setrlimit`, each individually
  configurable, with 0 meaning that limit is left unlimited:
  - Address space (`RLIMIT_AS`): 256 MB default (`DRIVER_RLIMIT_AS_BYTES`). This bounds
    virtual address space, so a library-heavy driver (numpy, pandas, BLAS) may need it
    raised or set to 0.
  - CPU time (`RLIMIT_CPU`): 60 seconds default (`DRIVER_RLIMIT_CPU_SECONDS`).
  - Open files (`RLIMIT_NOFILE`): 256 default (`DRIVER_RLIMIT_NOFILE`).
  - Processes (`RLIMIT_NPROC`): 1024 default (`DRIVER_RLIMIT_NPROC`). This is a
    per-UID ceiling that counts every thread the service user already holds
    container-wide, not just this child, so SSH/threaded drivers (netmiko,
    paramiko) need the headroom; it still guards against a runaway fork bomb.
- Device credentials are passed to the driver via a temporary file only; password-typed
  template fields are not copied into the child's environment variables.

A driver that exceeds the wall-clock timeout is recorded as failed with a timeout error.
A driver killed by a resource limit (for example out-of-memory or CPU exhaustion) is
recorded as failed with the terminating signal.

What the sandbox does NOT provide: there is no Linux namespace, seccomp, filesystem, or
network isolation, and the child runs as the same user as the execution service. A driver
can therefore open network connections and read files the service user can read. Driver
upload is restricted to admins for this reason; treat driver packages as trusted code.
The resource limits are POSIX-only and are not applied on platforms without `os.fork`.

### Dependencies

The execution image ships `netmiko` (which pulls `paramiko`), so SSH-based drivers
can `import netmiko` / `import paramiko` directly without vendoring or a runtime
`pip install`. netmiko gives network-CLI drivers config-mode handling, prompt
detection, and per-vendor platforms; a Management or L2/L3 driver that SSHes into a
device should use it rather than shipping its own SSH client.

For anything else, vendor your driver's third-party dependencies into a `_deps/`
directory inside the package; the execution service adds `_deps/` to the driver's
`PYTHONPATH` at runtime.

Installing a package `requirements.txt` at execution time is off by default, because a
runtime `pip install` pulls arbitrary code from the network as the service user. An
operator can opt in by setting `ALLOW_DRIVER_PIP_INSTALL=true`, in which case a package
that ships a `requirements.txt` (and has no `_deps/` yet) is installed once and cached
alongside the driver. When the flag is off, a `requirements.txt` is skipped with a log
line and the driver runs as-is; an import of a missing dependency then fails at runtime.

---

## Return values (Layer 1)

All driver methods must return a `dict`. The execution service serializes the return
value as JSON and stores it in the execution run record for audit purposes.

Minimum required keys per method:

| Method | Required keys |
|---|---|
| login | `{"success": bool}` |
| logout | `{"success": bool}` |
| connect_ports | `{"success": bool}` |
| disconnect_ports | `{"success": bool}` |
| status | `{"reachable": bool}` |

You may include additional keys for diagnostics, error messages, or device-specific
data. Example:

```python
def status(self) -> dict:
    return {
        "reachable": True,
        "firmware": "4.2.1",
        "uptime_hours": 1247,
        "port_count": 48,
    }
```

---

## Example: Calient S-Series L1 driver

```python
import socket


class Driver:
    """Calient S-Series optical L1 switch driver."""

    def __init__(self, context: dict):
        self.host = context["HERD_ip_address"]
        self.port = int(context.get("HERD_mgmt_port", 23))
        self.username = context["HERD_username"]
        self.password = context["HERD_password"]
        self.sock = None

    def login(self) -> dict:
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=10)
            self._send(f"login {self.username} {self.password}")
            response = self._recv()
            if "OK" not in response:
                return {"success": False, "message": response}
            return {"success": True}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def logout(self) -> dict:
        try:
            if self.sock:
                self._send("logout")
                self.sock.close()
            return {"success": True}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def connect_ports(self, port_a: str, port_b: str) -> dict:
        try:
            self._send(f"connect {port_a} {port_b}")
            response = self._recv()
            success = "OK" in response
            return {"success": success, "message": response}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def disconnect_ports(self, port_a: str, port_b: str) -> dict:
        try:
            self._send(f"disconnect {port_a} {port_b}")
            response = self._recv()
            success = "OK" in response
            return {"success": success, "message": response}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def status(self) -> dict:
        try:
            self._send("show system")
            response = self._recv()
            return {"reachable": True, "info": response}
        except Exception as e:
            return {"reachable": False, "message": str(e)}

    def _send(self, command: str):
        self.sock.sendall((command + "\n").encode())

    def _recv(self) -> str:
        return self.sock.recv(4096).decode().strip()
```

---

## Layer 2 Switch driver contract

Layer 2 switches manage VLAN membership on access ports connected to DUT devices. The
execution service calls L2 drivers when reservations are created or ended, provisioning
or deprovisioning VLANs that isolate reserved devices at Layer 2.

### Class definition

```python
class Driver:
    """Layer 2 Switch driver.

    The execution service instantiates this class once per operation batch,
    passing all device parameters and execution metadata in the context dict.
    """

    def __init__(self, context: dict):
        self.context = context

    def login(self) -> dict:
        """Establish a session with the switch.

        Returns:
            dict with at minimum {"success": bool}.
        """
        ...

    def logout(self) -> dict:
        """Tear down the session with the switch.

        Returns:
            dict with at minimum {"success": bool}.
        """
        ...

    def create_vlan(self, vlan_id: int) -> dict:
        """Create a VLAN on the switch.

        Called once per switch before adding ports to the VLAN.

        Args:
            vlan_id: VLAN ID (range 2-4094)

        Returns:
            dict with at minimum {"success": bool}.
        """
        ...

    def add_to_vlan(self, port: str, vlan_id: int, tag: str = "tagged") -> dict:
        """Add a switch port to a VLAN.

        Called once per DUT port connected through this switch.

        Args:
            port: switch port name (e.g. "eth1")
            vlan_id: VLAN ID to assign
            tag: "tagged" or "untagged"

        Returns:
            dict with at minimum {"success": bool}.
        """
        ...

    def remove_from_vlan(self, port: str, vlan_id: int) -> dict:
        """Remove a switch port from a VLAN.

        Called once per DUT port during deprovisioning.

        Args:
            port: switch port name
            vlan_id: VLAN ID to remove

        Returns:
            dict with at minimum {"success": bool}.
        """
        ...

    def delete_vlan(self, vlan_id: int) -> dict:
        """Delete a VLAN from the switch.

        Called once per switch after removing all ports from the VLAN.

        Args:
            vlan_id: VLAN ID to delete

        Returns:
            dict with at minimum {"success": bool}.
        """
        ...

    def status(self) -> dict:
        """Health check: verify the switch is reachable.

        Returns:
            dict with at minimum {"reachable": bool}.
        """
        ...
```

### When methods are called

| Event | Sequence |
|---|---|
| Reservation created (DUTs connected through L2 switch) | login(), create_vlan(vlan_id), add_to_vlan(port, vlan_id, tag) for each port, logout() |
| Reservation cancelled or completed | login(), remove_from_vlan(port, vlan_id) for each port, delete_vlan(vlan_id), logout() |
| Device added to HERD or admin health check | login(), status(), logout() |

Unlike L1 operations (which pair two ports), L2 operations are per-port: each DUT port
connected to an L2 switch yields one add_to_vlan or remove_from_vlan call.

When multiple DUT ports go through the same L2 switch in a single reservation, the
execution service batches them: one login/logout wrapping one create_vlan/delete_vlan
and all per-port add/remove calls.

### VLAN ID derivation

The execution service derives the VLAN ID deterministically from the reservation UUID:
`int(uuid.UUID(reservation_id).int % 4093) + 2`, producing values in the range 2-4094
(avoiding VLAN 1). Collisions are possible but acceptable for the current design.

### Port identity

The port argument is the L2 switch's own port name (as defined in the HERD inventory),
not the DUT port name. The cabling topology in HERD maps DUT ports to L2 switch ports;
the execution service resolves this mapping and passes only the switch-side port names
to the driver.

---

## Return values (Layer 2)

| Method | Required keys |
|---|---|
| login | `{"success": bool}` |
| logout | `{"success": bool}` |
| create_vlan | `{"success": bool}` |
| add_to_vlan | `{"success": bool}` |
| remove_from_vlan | `{"success": bool}` |
| delete_vlan | `{"success": bool}` |
| status | `{"reachable": bool}` |

---

## Future: Layer 3 Switch contract (planned)

```python
class Driver:
    def __init__(self, context: dict): ...
    def login(self) -> dict: ...
    def logout(self) -> dict: ...
    def configure_route(self, destination: str, next_hop: str, interface: str) -> dict: ...
    def remove_route(self, destination: str, next_hop: str, interface: str) -> dict: ...
    def status(self) -> dict: ...
```

## Management driver contract

The Management connection type is implemented (it is the worked example in the
packaging quickstart below). It is one of three connection types with an
AI-config schema today (Management, Layer 2 Switch, and Layer 3 Switch).

```python
class Driver:
    def __init__(self, context: dict): ...
    def login(self) -> dict: ...
    def logout(self) -> dict: ...
    def configure(self, **config) -> dict: ...
    def backup(self) -> dict: ...
    def status(self) -> dict: ...
```

### AI-generated configs are allowlisted (B8)

When `POST /api/ai/commit` is called with `apply_configs=true`, the AI
orchestrator validates each device's `config` against a JSON Schema registry
before calling `/execution/execute`. The `Management`, `Layer 2 Switch`, and
`Layer 3 Switch` connection types each have a schema; Management accepts the
keys `vlan` (integer 1-4094), `ip` (string), `hostname` (string), `description`
(string), while L2 carries a `vlan_assignments` shape and L3 carries
`interfaces`, `virtual_routers`, and `routes`. Anything outside the schema for a
device's connection type is rejected with 422 and the commit never writes to
cabling or reservations.

The schema registry lives in
`services/common/herd_common/device_config.py` (the `config_validator` module
in the ai-orchestrator service is a thin re-export of it). Adding a new
connection type's allowlist is a one-file change there. Drivers can also publish
their own config schema: a `config_schema()` classmethod on the `Driver` class
returning a JSON Schema dict is extracted (without instantiating the driver) via
the execution service and used to validate device configs, falling back to this
registry when a driver omits it or ships an unusable schema (issue #23). This
preference is not limited to the AI commit path: the execution service's
`configure` action validates the call's kwargs against the driver-published
schema first and only falls back to the registry on a `PublishedSchemaError`, so
a driver can accept a config vocabulary the registry would reject. Validation
runs after the driver is loaded, so the published schema is cached for the
current SHA. The checked-in `drivers/frr_mgmt/driver.py` is the worked example:
its `config_schema()` returns an object schema for `{commands, command}` (raw
vtysh config lines), which the registry's `additionalProperties: false`
Management schema would otherwise reject.

## Packaging quickstart

End-to-end: write a trivial `Management` driver, package it, upload it, wire it to a template, and run a method against a real device.

### 1. Project layout

```
my-driver/
  driver.py          # required, contains class Driver
  README.md          # optional
  requirements.txt   # optional; not installed by HERD, purely informational
```

Only `driver.py` at the package root is required; the Driver class must be defined there.

### 2. Minimal `driver.py`

```python
# driver.py
class Driver:
    def __init__(self, context: dict):
        self.ip = context.get("ip")
        self.login_name = context.get("login")
        self.password = context.get("password")

    def login(self) -> dict:
        # Open connection, authenticate, store a session handle on self.
        return {"success": True, "output": {"session": "opened"}}

    def logout(self) -> dict:
        return {"success": True, "output": {"session": "closed"}}

    def configure(self, **kwargs) -> dict:
        # kwargs come from method_kwargs; expect keys from the AI allowlist
        # (vlan, ip, hostname, description) for AI-driven calls.
        return {"success": True, "output": {"applied": kwargs}}

    def backup(self) -> dict:
        return {"success": True, "output": {"backup_ref": "snap-1"}}

    def status(self) -> dict:
        return {"success": True, "output": {"reachable": True}}
```

For a Management driver, every method must return a dict shaped `{success: bool, output: dict | None, error: str | None}`. (Layer 1 and Layer 2 drivers follow their own return-value contracts: see the L1 and L2 return-value tables earlier in this document, where `status` returns `{reachable: bool}` and the action methods return `{success: bool}`.) A raised exception is caught by the sandbox and surfaced as `success: False` with the exception text in `error`.

### 3. Package it

```bash
cd my-driver
zip -r my-driver.zip .
# or
tar czf my-driver.tar.gz .
```

Size cap: 10 MB.

### 4. Upload

In the admin UI: **Administration > Drivers > Upload**. Fill in name (unique), description, connection type (`Management` for this example), and the archive file.

Or via API:

```bash
curl -sS -X POST "https://<host>/api/inventory/drivers" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -F "name=my-driver" \
  -F "description=Demo Management driver" \
  -F "connection_type=Management" \
  -F "file=@my-driver.zip"
```

### 5. Create a template pointing at the driver

**Inventory > Templates > New template**, pick `device` type, select your new driver from the dropdown, add any field sections (`ip`, `login`, `password`, etc.), save.

### 6. Create a device

**Inventory > Add device**, pick the template, fill in field data, save.

### 7. Test-run a method

```bash
curl -sS -X POST "https://<host>/api/execution/execute" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "device_id": "<device-uuid>",
    "action": "status",
    "user_id": "<your-user-uuid>",
    "reservation_id": null
  }'
```

You should get back a run record with `status: SUCCESS` and your `{reachable: true}` output. Find it later via `GET /api/execution/runs/<run-id>`.

### 8. Iterate

Update `driver.py`, rebuild the archive, upload via **Drivers > Edit > Replace file** (or `PUT /api/inventory/drivers/{id}/file`). The execution service detects the new SHA256, invalidates its cache, and re-extracts on next call.

## Debugging tips

- **`TIMEOUT`**: your method took longer than `EXECUTION_TIMEOUT_SECONDS` (default 30). Raise it if the device is legitimately slow, or speed up the driver.
- **`FAILED` with `Driver class not found`**: the package root doesn't contain a `driver.py` with a `Driver` class, or the class is missing one of the required methods for its connection type.
- **`FAILED` with an exception traceback in `error`**: the driver method raised. Check the traceback; most common cause is a credentials or network issue inside `login()`.
- **Debugging locally**: run `python -c "from driver import Driver; d = Driver({...}); print(d.status())"` from the package dir. The execution service uses the same import path; if it works locally it will work in the sandbox.

