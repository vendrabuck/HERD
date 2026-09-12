"""Driver packages the seed uploads: inline stubs plus the real, checked-in
packages zipped straight from disk, and the get-or-create upload helper.
"""

import io
import os
import sys
import zipfile

import httpx

from .client import BASE, REPO_ROOT


def _make_dummy_zip(name: str) -> bytes:
    """Create a minimal in-memory zip file for driver upload."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("README.txt", f"Dummy driver package: {name}")
    return buf.getvalue()


def _make_management_demo_driver_zip() -> bytes:
    """Create a Management driver that simulates device liveness for screenshots.

    login always succeeds; status() returns reachable for ~3/4 of devices and
    raises for ~1/4 (those whose last IP octet is divisible by 4). The health
    scheduler records HEALTHY when status succeeds and DEGRADED when it raises,
    so a polled population lands ~3/4 HEALTHY and ~1/4 DEGRADED with no
    UNREACHABLE (login never fails). configure and backup are stubs present
    only to satisfy the Management connection type's required-method
    validation; the poller never calls them.
    """
    driver_code = '''\
try:
    from driver_transcript import record_command
except ImportError:
    def record_command(*args, **kwargs):
        pass


class Driver:
    """Demo Management driver: simulates liveness, no real network I/O."""

    def __init__(self, context):
        self.context = context

    def _is_degraded(self):
        ip = self.context.get("HERD_ip", "")
        try:
            last_octet = int(ip.rsplit(".", 1)[-1])
        except (ValueError, IndexError):
            last_octet = 1
        return last_octet % 4 == 0

    def login(self):
        record_command("login")
        return {"success": True}

    def logout(self):
        record_command("logout")
        return {"success": True}

    def configure(self, **kwargs):
        record_command("configure")
        return {"success": True}

    def backup(self):
        record_command("backup")
        return {"success": True}

    def status(self):
        if self._is_degraded():
            raise RuntimeError("status check failed: device not responding")
        record_command("show status", response="reachable")
        return {"reachable": True}
'''
    metadata = '{"supports_dry_run": false, "version": "1.0", "vendor": "HERD seed"}'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("driver.py", driver_code)
        zf.writestr("driver_metadata.json", metadata)
    return buf.getvalue()


def _make_l1_driver_zip() -> bytes:
    """Create a zip with a valid L1 switch driver that passes validate_driver()."""
    driver_code = '''\
try:
    from driver_transcript import record_command
except ImportError:
    def record_command(*args, **kwargs):
        pass


class Driver:
    """Seed L1 switch driver: satisfies all required methods for Layer 1 Switch."""

    def __init__(self, context):
        self.context = context
        self.dry_run = bool(context.get("dry_run", False))

    def _record(self, command, response="OK"):
        if self.dry_run:
            record_command(command, response="(simulated)", exit_status="simulated")
        else:
            record_command(command, response=response)

    def login(self):
        self._record("login")
        return {"success": True}

    def logout(self):
        self._record("logout")
        return {"success": True}

    def connect_ports(self, port_a, port_b):
        self._record(f"connect {port_a} {port_b}")
        return {
            "success": True,
            "port_a": port_a,
            "port_b": port_b,
            "simulated": self.dry_run,
        }

    def disconnect_ports(self, port_a, port_b):
        self._record(f"disconnect {port_a} {port_b}")
        return {
            "success": True,
            "port_a": port_a,
            "port_b": port_b,
            "simulated": self.dry_run,
        }

    def status(self):
        self._record("show status", response="reachable")
        return {"reachable": True}
'''
    metadata = '{"supports_dry_run": true, "version": "1.0", "vendor": "HERD seed"}'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("driver.py", driver_code)
        zf.writestr("driver_metadata.json", metadata)
    return buf.getvalue()


def _make_l2_driver_zip() -> bytes:
    """Create a zip with a valid L2 switch driver that passes validate_driver()."""
    driver_code = '''\
try:
    from driver_transcript import record_command
except ImportError:
    def record_command(*args, **kwargs):
        pass


class Driver:
    """Seed L2 switch driver: satisfies all required methods for Layer 2 Switch."""

    def __init__(self, context):
        self.context = context
        self.dry_run = bool(context.get("dry_run", False))

    def _record(self, command, response="OK"):
        if self.dry_run:
            record_command(command, response="(simulated)", exit_status="simulated")
        else:
            record_command(command, response=response)

    def login(self):
        self._record("login")
        return {"success": True}

    def logout(self):
        self._record("logout")
        return {"success": True}

    def create_vlan(self, vlan_id):
        self._record(f"vlan {vlan_id}")
        return {"success": True, "vlan_id": vlan_id, "simulated": self.dry_run}

    def add_to_vlan(self, port, vlan_id, tag="tagged"):
        self._record(
            f"interface {port}; switchport mode {tag}; switchport access vlan {vlan_id}"
        )
        return {
            "success": True,
            "port": port,
            "vlan_id": vlan_id,
            "tag": tag,
            "simulated": self.dry_run,
        }

    def remove_from_vlan(self, port, vlan_id):
        self._record(f"interface {port}; no switchport access vlan {vlan_id}")
        return {
            "success": True,
            "port": port,
            "vlan_id": vlan_id,
            "simulated": self.dry_run,
        }

    def delete_vlan(self, vlan_id):
        self._record(f"no vlan {vlan_id}")
        return {"success": True, "vlan_id": vlan_id, "simulated": self.dry_run}

    def status(self):
        self._record("show status", response="reachable")
        return {"reachable": True}
'''
    metadata = '{"supports_dry_run": true, "version": "1.0", "vendor": "HERD seed"}'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("driver.py", driver_code)
        zf.writestr("driver_metadata.json", metadata)
    return buf.getvalue()


def _make_cisco_6509_driver_zip() -> bytes:
    """Create a zip with a demo Cisco 6509 L2 driver that passes validate_driver()."""
    driver_code = '''\
try:
    from driver_transcript import record_command
except ImportError:
    def record_command(*args, **kwargs):
        pass


class Driver:
    """Demo Cisco Catalyst 6509 Layer 2 driver (stub, no network I/O)."""

    def __init__(self, context):
        self.context = context
        self.dry_run = bool(context.get("dry_run", False))

    def _record(self, command, response="OK"):
        if self.dry_run:
            record_command(command, response="(simulated)", exit_status="simulated")
        else:
            record_command(command, response=response)

    def login(self):
        self._record("enable", response=">")
        self._record("configure terminal", response="(config)#")
        return {"success": True}

    def logout(self):
        self._record("end", response="#")
        self._record("exit", response="")
        return {"success": True}

    def create_vlan(self, vlan_id):
        self._record(f"vlan {vlan_id}", response="(config-vlan)#")
        return {"success": True, "vlan_id": vlan_id, "simulated": self.dry_run}

    def add_to_vlan(self, port, vlan_id, tag="tagged"):
        self._record(f"interface {port}", response="(config-if)#")
        self._record(f"switchport mode {tag}", response="(config-if)#")
        self._record(f"switchport access vlan {vlan_id}", response="(config-if)#")
        return {
            "success": True,
            "port": port,
            "vlan_id": vlan_id,
            "tag": tag,
            "simulated": self.dry_run,
        }

    def remove_from_vlan(self, port, vlan_id):
        self._record(f"interface {port}", response="(config-if)#")
        self._record(f"no switchport access vlan {vlan_id}", response="(config-if)#")
        return {
            "success": True,
            "port": port,
            "vlan_id": vlan_id,
            "simulated": self.dry_run,
        }

    def delete_vlan(self, vlan_id):
        self._record(f"no vlan {vlan_id}", response="(config)#")
        return {"success": True, "vlan_id": vlan_id, "simulated": self.dry_run}

    def status(self):
        self._record("show version", response="Cisco IOS")
        return {"reachable": True}
'''
    metadata = (
        '{"supports_dry_run": true, "version": "1.0", "vendor": "Cisco", "notes": "demo stub"}'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("driver.py", driver_code)
        zf.writestr("driver_metadata.json", metadata)
    return buf.getvalue()


# Path to the real, checked-in FRR management driver package (repo root: drivers/frr_mgmt/).
# Unlike the other seed drivers, this one is NOT an inline stub: it is the genuine netmiko
# driver used in the live-config demo, zipped straight from disk so the seeded package and
# the source of truth can never drift.
FRR_DRIVER_DIR = os.path.join(REPO_ROOT, "drivers", "frr_mgmt")


def _make_frr_driver_zip() -> bytes | None:
    """Zip the real drivers/frr_mgmt/ package from disk.

    Returns None (and prints a warning) if the package is missing, so a checkout
    without the driver still seeds everything else instead of hard-failing. Only
    driver.py and driver_metadata.json are packaged; __pycache__ and other cruft
    are skipped so the SHA256 cache key is stable across runs.
    """
    driver_py = os.path.join(FRR_DRIVER_DIR, "driver.py")
    metadata_json = os.path.join(FRR_DRIVER_DIR, "driver_metadata.json")
    if not (os.path.isfile(driver_py) and os.path.isfile(metadata_json)):
        print(f"  WARN: FRR driver package not found at {FRR_DRIVER_DIR}; skipping FRR seed")
        return None
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        with open(driver_py, encoding="utf-8") as f:
            zf.writestr("driver.py", f.read())
        with open(metadata_json, encoding="utf-8") as f:
            zf.writestr("driver_metadata.json", f.read())
    return buf.getvalue()


def _make_driver_zip_from_dir(driver_dir: str) -> bytes | None:
    """Zip a real, checked-in driver package (driver.py + driver_metadata.json)
    straight from disk. A generalized _make_frr_driver_zip: the srl_l2 and
    frr_l3 NOS-lab drivers share this so the seeded package can never drift
    from the source of truth, the same reasoning _make_frr_driver_zip's
    docstring gives for drivers/frr_mgmt.

    Returns None (and prints a warning) if the package is missing, so a
    checkout without a driver still seeds everything else instead of hard
    failing. Only driver.py and driver_metadata.json are packaged; __pycache__
    and other cruft are skipped so the SHA256 cache key is stable across runs.
    """
    driver_py = os.path.join(driver_dir, "driver.py")
    metadata_json = os.path.join(driver_dir, "driver_metadata.json")
    if not (os.path.isfile(driver_py) and os.path.isfile(metadata_json)):
        print(f"  WARN: driver package not found at {driver_dir}; skipping NOS lab seed")
        return None
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        with open(driver_py, encoding="utf-8") as f:
            zf.writestr("driver.py", f.read())
        with open(metadata_json, encoding="utf-8") as f:
            zf.writestr("driver_metadata.json", f.read())
    return buf.getvalue()


def get_or_create_driver(
    client: httpx.Client,
    name: str,
    connection_type: str,
    zip_bytes: bytes | None = None,
    replace_if_exists: bool = False,
) -> str:
    """Create a driver package or return the existing one's id.

    When replace_if_exists is set and the driver already exists, the package
    FILE is replaced (PUT /drivers/{id}/file) with the supplied zip_bytes so a
    re-seed picks up an updated driver.py and its new SHA. Off by default so the
    heavy default seed never churns stable driver packages.
    """
    if zip_bytes is None:
        zip_bytes = _make_dummy_zip(name)
    filename = name.lower().replace(" ", "_") + ".zip"
    resp = client.post(
        f"{BASE}/inventory/drivers",
        data={"name": name, "connection_type": connection_type, "description": f"{name} driver"},
        files={"file": (filename, zip_bytes, "application/zip")},
    )
    if resp.status_code == 201:
        did = resp.json()["id"]
        print(f"  Created driver: {name} ({did})")
        return did

    # Already exists (409); look it up
    listing = client.get(f"{BASE}/inventory/drivers", params={"limit": 500})
    for d in listing.json()["items"]:
        if d["name"] == name:
            did = d["id"]
            if replace_if_exists:
                put = client.put(
                    f"{BASE}/inventory/drivers/{did}/file",
                    files={"file": (filename, zip_bytes, "application/zip")},
                )
                if put.status_code == 200:
                    print(f"  Replaced driver package: {name} ({did})")
                else:
                    print(
                        f"  WARNING: could not replace driver package {name} "
                        f"({put.status_code}): {put.text}"
                    )
            else:
                print(f"  Exists driver: {name} ({did})")
            return did

    print(f"  Failed to create or find driver {name}: {resp.text}")
    sys.exit(1)
