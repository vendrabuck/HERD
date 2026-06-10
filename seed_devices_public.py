#!/usr/bin/env python3
"""Seed script: users, drivers, templates, devices, ports, L1/L2 switches, cabling, groups.

Creates:
  - 50 admin users and 1000 regular users
  - 4 drivers: Network OS Management, Endpoint Management, L1 Switch Driver, L2 Switch Driver
  - 28 DUT device templates (24 generic network devices across 6 invented vendors
    in 6 classes: router, switch, firewall, load balancer, server, storage + 4 client OS)
  - 4 infra templates: 1 L1 switch, 2 L2 switch (incl. Cisco Catalyst 6509), 1 port template
  - 5000 DUT devices distributed evenly across templates (variable port counts per template)
  - 29 L1 switches: 27 edge (9 per lab) + 2 hub, each with 256 backplane ports (0/slot/port)
  - 30 L2 edge switches (10 per lab), each with 48 access ports
  - L1 cabling: DUT-to-edge (eth1-eth5/eth1-eth2) + 54 edge-to-hub + 1 hub-to-hub
  - L2 cabling: DUT-to-L2 (network-device eth6-eth7, client eth3) round-robin across L2 switches
  - 6 isolated demo devices (zero cabling, TEST-NET-1 IPs) as known-unreachable endpoints
  - 60 demo lab topologies: 50 valid named shapes (chain, star, ring, mesh, dual-homed)
    plus 10 deliberately invalid (no_path and missing_device) for editor demos
  - 3 device groups: Lab Alpha, Lab Bravo, Lab Charlie (DUTs + L1 + L2 switches)
  - 5 user groups: Lab Admins, Lab Managers, Lab Alpha Tier 1, Lab Bravo Tier 1, Lab Charlie Tier 1
  - Device group permissions: Lab Alpha Tier 1 to Lab Alpha, Lab Bravo Tier 1 to Lab Bravo,
    Lab Charlie Tier 1 to Lab Charlie; admin groups get no permissions (admin role sees all)

Re-runnable: skips resources that already exist.
"""

import io
import os
import sys
import zipfile

import httpx
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE = os.environ.get("SEED_BASE_URL", "https://localhost/api")
# Credential resolution mirrors the test harnesses (tests/integration/conftest.py,
# tests/load/locustfile.py, tests/e2e/conftest.py): an explicit SEED_* takes priority
# (the Makefile exports these), then the stack's actual bootstrap SUPERADMIN_* from .env,
# then a generic non-personal placeholder. Never hardcode a real address here.
EMAIL = os.environ.get("SEED_EMAIL") or os.environ.get("SUPERADMIN_EMAIL", "admin@example.com")
PASSWORD = os.environ.get("SEED_PASSWORD") or os.environ.get("SUPERADMIN_PASSWORD", "admin123!")

SECTIONS = [
    {
        "name": "Network",
        "fields": [
            {"key": "ip", "label": "IP Address", "type": "string", "required": True},
            {"key": "login", "label": "Login", "type": "string", "required": True},
            {"key": "password", "label": "Password", "type": "password", "required": True},
        ],
    }
]

CLIENT_SECTIONS = [
    {
        "name": "Network",
        "fields": [
            {"key": "ip", "label": "IP Address", "type": "string", "required": True},
            {"key": "login", "label": "Login", "type": "string", "required": True},
            {"key": "password", "label": "Password", "type": "password", "required": True},
        ],
    },
    {
        "name": "System",
        "fields": [
            {"key": "hostname", "label": "Hostname", "type": "string", "required": False},
            {"key": "os_version", "label": "OS Version", "type": "string", "required": False},
        ],
    },
]

PORT_SECTIONS = [{"name": "Port Info", "fields": []}]

# Generic, invented hardware catalog. None of these vendors or models are real;
# they exist so the seeded inventory looks like a heterogeneous lab without
# referencing any real company's gear. Six invented vendors, each shipping one
# model in four device classes (router, switch, firewall, load balancer, server,
# storage), for 24 network-device templates. The 5000 DUT devices distribute
# evenly across every template (see generate_devices), so adding or removing
# entries here re-weights the fleet automatically.
#
# Each tuple is (vendor, device_class, model_code, name_prefix).
GENERIC_CATALOG = [
    ("Aerendir", "Router", "RTR-9000", "AER-RTR"),
    ("Aerendir", "Switch", "SW-4800", "AER-SW"),
    ("Aerendir", "Firewall", "FW-2200", "AER-FW"),
    ("Aerendir", "Load Balancer", "LB-700", "AER-LB"),
    ("Cobalt", "Router", "CR-8100", "COB-RTR"),
    ("Cobalt", "Switch", "CS-3600", "COB-SW"),
    ("Cobalt", "Server", "SRV-1U", "COB-SRV"),
    ("Cobalt", "Storage", "STG-4000", "COB-STG"),
    ("Meridian", "Router", "MX-7500", "MER-RTR"),
    ("Meridian", "Switch", "MS-2400", "MER-SW"),
    ("Meridian", "Firewall", "MF-1800", "MER-FW"),
    ("Meridian", "Load Balancer", "ML-500", "MER-LB"),
    ("Vantage", "Router", "VR-6200", "VAN-RTR"),
    ("Vantage", "Switch", "VS-4810", "VAN-SW"),
    ("Vantage", "Server", "VSV-2U", "VAN-SRV"),
    ("Vantage", "Storage", "VST-8000", "VAN-STG"),
    ("Helix", "Router", "HR-5400", "HLX-RTR"),
    ("Helix", "Switch", "HS-1600", "HLX-SW"),
    ("Helix", "Firewall", "HF-3300", "HLX-FW"),
    ("Helix", "Load Balancer", "HL-900", "HLX-LB"),
    ("Northwind", "Router", "NR-7700", "NW-RTR"),
    ("Northwind", "Switch", "NS-4820", "NW-SW"),
    ("Northwind", "Server", "NSV-4U", "NW-SRV"),
    ("Northwind", "Storage", "NST-12000", "NW-STG"),
]


def _network_template(vendor: str, device_class: str, model: str) -> dict:
    name = f"{vendor} {model}"
    return {
        "name": name,
        "description": f"{vendor} {device_class}, {model}",
        "vendor": vendor,
        "model": model,
        "sections": SECTIONS,
    }


# 24 generic network-device templates + 4 client OS templates.
DEVICE_TEMPLATES = [
    _network_template(vendor, device_class, model)
    for vendor, device_class, model, _prefix in GENERIC_CATALOG
] + [
    {
        "name": "Windows 10 Client",
        "description": "Windows 10 endpoint",
        "vendor": "Microsoft",
        "model": "Windows 10",
        "sections": CLIENT_SECTIONS,
    },
    {
        "name": "Windows 11 Client",
        "description": "Windows 11 endpoint",
        "vendor": "Microsoft",
        "model": "Windows 11",
        "sections": CLIENT_SECTIONS,
    },
    {
        "name": "macOS Client",
        "description": "macOS endpoint",
        "vendor": "Apple",
        "model": "macOS",
        "sections": CLIENT_SECTIONS,
    },
    {
        "name": "Ubuntu Client",
        "description": "Ubuntu Linux endpoint",
        "vendor": "Canonical",
        "model": "Ubuntu",
        "sections": CLIENT_SECTIONS,
    },
]

# Device name prefixes for each template.
TEMPLATE_PREFIX = {f"{vendor} {model}": prefix for vendor, _class, model, prefix in GENERIC_CATALOG}
TEMPLATE_PREFIX.update(
    {
        "Windows 10 Client": "Win10",
        "Windows 11 Client": "Win11",
        "macOS Client": "macOS",
        "Ubuntu Client": "Ubuntu",
    }
)

TOTAL_DEVICES = 5000
NUM_ADMINS = 50
NUM_USERS = 1000

# Health-polling demo subset (for screenshots). Only these devices opt into
# periodic liveness polling, via a per-device poll_interval_seconds, so a fresh
# environment populates health badges within minutes instead of polling all
# 5000 devices (the scheduler ceiling is ~20 devices/min). The L1/L2 hub
# switches are enrolled separately in main(). Devices not listed here stay
# unpolled and show no health badge.
POLL_INTERVAL_SECONDS = 60
POLL_SUBSET_COUNTS = {
    "Aerendir RTR-9000": 50,
    "Meridian MX-7500": 50,
    "Northwind NR-7700": 50,
    "Windows 10 Client": 50,
}

# Port counts per template. Network-device models are unlisted and fall through
# to DEFAULT_PORT_COUNT (32), which leaves room for the cabling pass to use
# eth1-eth5 (L1) and eth6-eth7 (L2). Clients are small and listed explicitly.
TEMPLATE_PORT_COUNTS = {
    "Windows 10 Client": 10,  # 2 L1 + 1 L2
    "Windows 11 Client": 10,  # 2 L1 + 1 L2
    "macOS Client": 3,  # 2 L1 + 1 L2
    "Ubuntu Client": 3,  # 2 L1 + 1 L2
}
DEFAULT_PORT_COUNT = 32

# L1 switch infrastructure
L1_SWITCHES_PER_LAB = 9
NUM_EDGE_SWITCHES = L1_SWITCHES_PER_LAB * 3  # 27
NUM_HUB_SWITCHES = 2
NUM_L1_SWITCHES = NUM_EDGE_SWITCHES + NUM_HUB_SWITCHES  # 29
L1_SLOTS = 8
L1_PORTS_PER_SLOT = 32
L1_PORTS_TOTAL = L1_SLOTS * L1_PORTS_PER_SLOT  # 256

# L2 switch infrastructure
L2_SWITCHES_PER_LAB = 10
NUM_L2_SWITCHES = L2_SWITCHES_PER_LAB * 3  # 30
NUM_L2_HUB_SWITCHES = 2
L2_PORTS_TOTAL = 48
L2_HUB_PORTS_TOTAL = 48
L2_DUT_PORTS_MAX = 46  # reserve eth47, eth48 on edges for hub uplinks
L2_PORTS_PER_PA = 6  # eth6, eth7 (after L1 ports eth1-eth5)
L2_PORTS_PER_CLIENT = 2  # eth3 (after L1 ports eth1-eth2)

# Lab topology demo seeding. A topology is a first-class object in the cabling
# service (a named React Flow canvas of device nodes and edges). We seed exactly
# 50 valid named lab topologies plus 10 deliberately invalid ones so a fresh
# environment shows what valid and invalid topologies look like and gives the
# editor realistic demo content. Validity is decided server-side by BFS over the
# physical connection graph: an edge is valid iff a path exists between its two
# devices. The seeded fabric is one connected component (all L1/L2 edges share
# the hub switches), so any two CABLED devices are reachable; to force an invalid
# no_path edge we cable nothing to a small set of dedicated isolated demo devices
# and use them as known-unreachable endpoints. A node with no device id forces
# the other invalid reason, missing_device. Generation is pure index math (no
# randomness) so the script stays re-runnable.
VALID_TOPOLOGY_TARGET = 50
INVALID_TOPOLOGY_TARGET = 10
NUM_ISOLATED_DEMO_DEVICES = 6
ISOLATED_DEMO_IP_BASE = "192.0.2."  # TEST-NET-1 (RFC 5737); disjoint from 10.x and 172.16.x


def fetch_all_items(
    client: httpx.Client,
    url: str,
    params: dict | None = None,
    page_size: int = 500,
) -> list[dict]:
    """Fetch all items from a paginated endpoint."""
    params = dict(params or {})
    items: list[dict] = []
    skip = 0
    while True:
        params["skip"] = skip
        params["limit"] = page_size
        resp = client.get(url, params=params)
        if resp.status_code != 200:
            print(f"  fetch_all_items failed for {url} ({resp.status_code}): {resp.text}")
            return items
        data = resp.json()
        items.extend(data["items"])
        if skip + page_size >= data["total"]:
            break
        skip += page_size
    return items


def generate_ip_list(count: int) -> list[str]:
    """Generate IPs starting at 10.0.0.2, going to .254, then incrementing the third octet."""
    ips: list[str] = []
    octet3 = 0
    octet4 = 2
    for _ in range(count):
        ips.append(f"10.0.{octet3}.{octet4}")
        octet4 += 1
        if octet4 > 254:
            octet4 = 2
            octet3 += 1
    return ips


def generate_devices() -> list[dict]:
    """Distribute devices evenly across templates and assign IPs."""
    template_names = [t["name"] for t in DEVICE_TEMPLATES]
    num_templates = len(template_names)
    base_count = TOTAL_DEVICES // num_templates
    remainder = TOTAL_DEVICES % num_templates

    # Build per-template counts
    counts: list[int] = []
    for i in range(num_templates):
        counts.append(base_count + (1 if i < remainder else 0))

    ips = generate_ip_list(TOTAL_DEVICES)
    devices: list[dict] = []
    ip_idx = 0
    for tmpl_name, count in zip(template_names, counts):
        prefix = TEMPLATE_PREFIX[tmpl_name]
        poll_quota = POLL_SUBSET_COUNTS.get(tmpl_name, 0)
        for i in range(count):
            ip = ips[ip_idx]
            dev = {
                "name": f"{prefix} - {ip}",
                "template": tmpl_name,
                "ip": ip,
            }
            if i < poll_quota:
                dev["poll_interval_seconds"] = POLL_INTERVAL_SECONDS
            devices.append(dev)
            ip_idx += 1

    return devices


def generate_users() -> list[dict]:
    """Generate 50 admins and 1000 regular users."""
    users: list[dict] = []
    for i in range(1, NUM_ADMINS + 1):
        users.append(
            {
                "username": f"admin{i}",
                "email": f"admin{i}@herd.dev",
                "password": f"admin{i}admin{i}",
                "role": "admin",
            }
        )
    for i in range(1, NUM_USERS + 1):
        users.append(
            {
                "username": f"user{i}",
                "email": f"user{i}@herd.dev",
                "password": f"user{i}user{i}xx",
                "role": None,
            }
        )
    return users


SEED_USERS = generate_users()
DEVICES = generate_devices()


def login(client: httpx.Client) -> str:
    resp = client.post(f"{BASE}/auth/login", json={"email": EMAIL, "password": PASSWORD})
    if resp.status_code != 200:
        print(f"Login failed: {resp.text}")
        sys.exit(1)
    token = resp.json()["access_token"]
    print(f"Logged in as {EMAIL}")
    return token


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
FRR_DRIVER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "drivers", "frr_mgmt")


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


# The live FRR routers in the network-simulator slice1 lab (topologies/slice1.clab.yml).
# r1/r2 run frrouting/frr with an SSH-to-vtysh login (netadmin/demo123, shell /usr/bin/vtysh)
# on the 10.99.0.0/24 management plane. These are the real targets the FRR driver configures
# in the Thursday live-config demo. demo123 is a throwaway lab password, not a secret.
FRR_LAB_DEVICES = [
    {"name": "frr-r1", "ip": "10.99.0.11"},
    {"name": "frr-r2", "ip": "10.99.0.12"},
]
FRR_LAB_LOGIN = os.environ.get("SEED_FRR_LOGIN", "netadmin")
FRR_LAB_PASSWORD = os.environ.get("SEED_FRR_PASSWORD", "demo123")


def get_or_create_management_device(
    client: httpx.Client,
    name: str,
    template_id: str,
    ip: str,
    login: str,
    password: str,
) -> str:
    """Create (or find) a Management device with explicit SSH credentials.

    Unlike get_or_create_device, which hardcodes generic admin creds for the bulk
    DUT population, this threads real per-device creds into field_data so the
    execution sandbox hands the driver working HERD_login / HERD_password.
    """
    field_data = {"ip": ip, "login": login, "password": password}
    body = {
        "name": name,
        "template_id": template_id,
        "topology_type": "PHYSICAL",
        "field_data": field_data,
    }
    resp = client.post(f"{BASE}/inventory/devices", json=body)
    if resp.status_code == 201:
        return resp.json()["id"]
    if resp.status_code == 409:
        for d in fetch_all_items(client, f"{BASE}/inventory/devices"):
            if d["name"] == name:
                return d["id"]
    print(f"  Failed to create or find device {name}: {resp.text}")
    sys.exit(1)


def seed_frr_demo(client: httpx.Client) -> None:
    """Register the real FRR netmiko driver and the two live lab routers.

    Idempotent and opt-in (gated by SEED_FRR=1 in main) so the heavy default seed
    is unchanged. This is the spine of the live-config demo: HERD drives a real FRR
    router over SSH via vtysh. The devices point at the network-simulator slice1 lab
    (10.99.0.11/.12); they are reachable only when that lab is up on the 10.99.0.0/24
    subnet, which is expected (the driver's status() reports unreachable otherwise).
    """
    print("\n--- FRR live-config demo ---")
    zip_bytes = _make_frr_driver_zip()
    if zip_bytes is None:
        return  # warning already printed; nothing to seed without the package

    frr_driver_id = get_or_create_driver(
        client,
        "FRRouting Management Driver",
        "Management",
        zip_bytes=zip_bytes,
    )
    frr_template_id = get_or_create_template(
        client,
        "FRRouting Router",
        "device",
        "FRRouting router driven over SSH via vtysh (netmiko). Live-config demo target.",
        SECTIONS,
        driver_id=frr_driver_id,
        vendor="FRRouting",
        model="FRR (slice1 lab)",
    )
    for dev in FRR_LAB_DEVICES:
        device_id = get_or_create_management_device(
            client,
            dev["name"],
            frr_template_id,
            dev["ip"],
            FRR_LAB_LOGIN,
            FRR_LAB_PASSWORD,
        )
        print(f"  FRR device: {dev['name']} -> {dev['ip']} ({device_id})")


def get_or_create_driver(
    client: httpx.Client,
    name: str,
    connection_type: str,
    zip_bytes: bytes | None = None,
) -> str:
    """Create a driver package or return the existing one's id."""
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
            print(f"  Exists driver: {name} ({d['id']})")
            return d["id"]

    print(f"  Failed to create or find driver {name}: {resp.text}")
    sys.exit(1)


def get_or_create_template(
    client: httpx.Client,
    name: str,
    template_type: str,
    description: str,
    sections: list,
    driver_id: str | None = None,
    exclusive: bool | None = None,
    vendor: str | None = None,
    model: str | None = None,
    part_number: str | None = None,
    poll_interval_seconds: int | None = None,
) -> str:
    body: dict = {
        "name": name,
        "template_type": template_type,
        "description": description,
        "sections": sections,
    }
    if driver_id is not None:
        body["driver_id"] = driver_id
    if exclusive is not None:
        body["exclusive"] = exclusive
    if vendor is not None:
        body["vendor"] = vendor
    if model is not None:
        body["model"] = model
    if part_number is not None:
        body["part_number"] = part_number
    if poll_interval_seconds is not None:
        body["poll_interval_seconds"] = poll_interval_seconds
    resp = client.post(f"{BASE}/inventory/templates", json=body)
    if resp.status_code == 201:
        tid = resp.json()["id"]
        print(f"  Created template: {name} ({tid})")
        return tid

    # Already exists; look it up
    listing = client.get(
        f"{BASE}/inventory/templates",
        params={"template_type": template_type, "limit": 500},
    )
    for t in listing.json()["items"]:
        if t["name"] == name:
            print(f"  Exists template: {name} ({t['id']})")
            return t["id"]

    print(f"  Failed to create or find template {name}: {resp.text}")
    sys.exit(1)


def get_or_create_device(
    client: httpx.Client,
    name: str,
    template_id: str,
    ip: str,
    poll_interval_seconds: int | None = None,
) -> str:
    field_data = {"ip": ip, "login": "admin", "password": "admin123"}
    body: dict = {
        "name": name,
        "template_id": template_id,
        "topology_type": "PHYSICAL",
        "field_data": field_data,
    }
    if poll_interval_seconds is not None:
        body["poll_interval_seconds"] = poll_interval_seconds
    resp = client.post(
        f"{BASE}/inventory/devices",
        json=body,
    )
    if resp.status_code == 201:
        did = resp.json()["id"]
        return did

    if resp.status_code == 409:
        # Already exists; look it up
        all_devices = fetch_all_items(client, f"{BASE}/inventory/devices")
        for d in all_devices:
            if d["name"] == name:
                return d["id"]

    print(f"  Failed to create or find device {name}: {resp.text}")
    sys.exit(1)


def create_ports(
    client: httpx.Client,
    device_id: str,
    port_template_id: str,
    count: int = 32,
    prefix: str = "eth",
) -> None:
    existing = client.get(f"{BASE}/inventory/devices/{device_id}/ports")
    if existing.status_code == 200 and len(existing.json()) > 0:
        return

    # Bulk create endpoint accepts max 200 per call
    created = 0
    while created < count:
        batch = min(200, count - created)
        client.post(
            f"{BASE}/inventory/devices/{device_id}/ports/bulk",
            json={
                "name_prefix": prefix,
                "starting_index": created + 1,
                "instances": batch,
                "template_id": port_template_id,
                "field_data": {},
            },
        )
        created += batch


def get_or_create_group(client: httpx.Client, name: str, description: str) -> str:
    resp = client.post(
        f"{BASE}/auth/groups",
        json={"name": name, "description": description},
    )
    if resp.status_code == 201:
        gid = resp.json()["id"]
        print(f"  Created group: {name} ({gid})")
        return gid

    # Already exists (409); look it up
    listing = client.get(f"{BASE}/auth/groups", params={"limit": 500})
    if listing.status_code != 200:
        print(f"  GET /auth/groups failed ({listing.status_code}): {listing.text}")
        sys.exit(1)
    for g in listing.json()["items"]:
        if g["name"] == name:
            print(f"  Exists group: {name} ({g['id']})")
            return g["id"]

    print(f"  Failed to create or find group {name}: {resp.text}")
    sys.exit(1)


def get_user_id(client: httpx.Client, username: str) -> str | None:
    all_users = fetch_all_items(client, f"{BASE}/auth/users")
    for u in all_users:
        if u["username"] == username:
            return u["id"]
    return None


def get_all_user_ids(client: httpx.Client) -> dict[str, str]:
    """Fetch all users once and return username to id mapping."""
    all_users = fetch_all_items(client, f"{BASE}/auth/users")
    return {u["username"]: u["id"] for u in all_users}


def add_group_member(client: httpx.Client, group_id: str, user_id: str) -> None:
    resp = client.post(
        f"{BASE}/auth/groups/{group_id}/members",
        json={"user_id": user_id},
    )
    if resp.status_code not in (201, 409):
        print(f"    Failed to add member: {resp.text}")


def get_or_create_device_group(client: httpx.Client, name: str, description: str) -> str:
    resp = client.post(
        f"{BASE}/inventory/device-groups",
        json={"name": name, "description": description},
    )
    if resp.status_code == 201:
        gid = resp.json()["id"]
        print(f"  Created device group: {name} ({gid})")
        return gid

    # Already exists (409); look it up
    listing = client.get(f"{BASE}/inventory/device-groups", params={"limit": 500})
    if listing.status_code != 200:
        print(f"  GET /inventory/device-groups failed ({listing.status_code}): {listing.text}")
        sys.exit(1)
    for g in listing.json()["items"]:
        if g["name"] == name:
            print(f"  Exists device group: {name} ({g['id']})")
            return g["id"]

    print(f"  Failed to create or find device group {name}: {resp.text}")
    sys.exit(1)


def bulk_remove_from_group(
    client: httpx.Client, group_id: str, user_ids: list[str], batch_size: int = 500
) -> None:
    """Remove users from a user group in batches."""
    total = len(user_ids)
    for start in range(0, total, batch_size):
        batch = user_ids[start : start + batch_size]
        resp = client.post(
            f"{BASE}/auth/groups/{group_id}/members/bulk-remove",
            json={"user_ids": batch},
        )
        done = min(start + batch_size, total)
        if resp.status_code == 200:
            data = resp.json()
            removed = data["removed"]
            not_found = data["not_found"]
            print(f"    Batch {done}/{total}: removed={removed}, not_found={not_found}")
        else:
            print(f"    Batch {done}/{total}: failed ({resp.status_code})")


def bulk_remove_devices_from_group(
    client: httpx.Client, group_id: str, device_ids: list[str], batch_size: int = 500
) -> None:
    """Remove devices from a device group in batches."""
    total = len(device_ids)
    for start in range(0, total, batch_size):
        batch = device_ids[start : start + batch_size]
        resp = client.post(
            f"{BASE}/inventory/device-groups/{group_id}/devices/bulk-remove",
            json={"device_ids": batch},
        )
        done = min(start + batch_size, total)
        if resp.status_code == 200:
            data = resp.json()
            removed = data["removed"]
            not_found = data["not_found"]
            print(f"    Batch {done}/{total}: removed={removed}, not_found={not_found}")
        else:
            print(f"    Batch {done}/{total}: failed ({resp.status_code})")


def bulk_add_devices_to_group(
    client: httpx.Client, group_id: str, device_ids: list[str], batch_size: int = 500
) -> None:
    """Add devices to a device group in batches."""
    total = len(device_ids)
    for start in range(0, total, batch_size):
        batch = device_ids[start : start + batch_size]
        resp = client.post(
            f"{BASE}/inventory/device-groups/{group_id}/devices/bulk",
            json={"device_ids": batch},
        )
        done = min(start + batch_size, total)
        if resp.status_code == 200:
            data = resp.json()
            print(f"    Batch {done}/{total}: added={data['added']}, skipped={data['skipped']}")
        else:
            print(f"    Batch {done}/{total}: failed ({resp.status_code})")


def bulk_add_permissions_to_device_group(
    client: httpx.Client,
    device_group_id: str,
    user_group_ids: list[str],
) -> None:
    """Add user group permissions to a device group."""
    resp = client.post(
        f"{BASE}/inventory/device-groups/{device_group_id}/permissions/bulk",
        json={"user_group_ids": user_group_ids},
    )
    if resp.status_code == 200:
        data = resp.json()
        print(f"    Permissions: added={data['added']}, skipped={data['skipped']}")
    else:
        print(f"    Permissions failed ({resp.status_code}): {resp.text}")


def create_l1_switch_ports(
    client: httpx.Client,
    device_id: str,
    port_template_id: str,
) -> None:
    """Create 256 ports on an L1 switch using backplane naming: 0/slot/port."""
    existing = client.get(f"{BASE}/inventory/devices/{device_id}/ports")
    if existing.status_code == 200 and len(existing.json()) > 0:
        return

    for slot in range(L1_SLOTS):
        client.post(
            f"{BASE}/inventory/devices/{device_id}/ports/bulk",
            json={
                "name_prefix": f"0/{slot}/",
                "starting_index": 1,
                "instances": L1_PORTS_PER_SLOT,
                "template_id": port_template_id,
                "field_data": {},
            },
        )


def create_connections(
    client: httpx.Client,
    pa_device_ids: list[str],
    client_device_ids: list[str],
    edge_switch_ids: list[str],
    hub_switch_ids: list[str],
) -> int:
    """Create all cabling connections: DUT-to-edge, edge-to-hub, hub-to-hub.

    PA devices get up to 5 connections (eth1-eth5), client devices get 2 (eth1-eth2).
    Edge switches fill until slots 0-6 are exhausted; remaining DUTs are skipped.

    Returns total connections created.
    """
    PORTS_PER_PA = 6
    PORTS_PER_CLIENT = 2

    # Re-runnability guard: check if first edge switch already has connections
    if edge_switch_ids:
        resp = client.get(
            f"{BASE}/cabling/connections",
            params={"device_id": edge_switch_ids[0], "limit": 1},
        )
        if resp.status_code == 200:
            data = resp.json()
            total = data.get("total", len(data.get("items", [])))
            if total > 0:
                print("  Connections already exist, skipping cabling")
                return 0

    created = 0

    # DUT-to-edge: round-robin DUTs across edge switches
    # Each edge switch uses slots 0-6 (224 ports) for DUT connections
    dut_ports_per_switch = 7 * L1_PORTS_PER_SLOT  # 224
    edge_port_counters = [0] * len(edge_switch_ids)

    total_pa = len(pa_device_ids)
    print(
        f"  Creating PA-to-edge connections ({total_pa} PA devices, {PORTS_PER_PA} ports each)..."
    )
    for i, dut_id in enumerate(pa_device_ids):
        edge_idx = i % len(edge_switch_ids)
        for port_idx in range(PORTS_PER_PA):
            port_num = edge_port_counters[edge_idx]
            if port_num >= dut_ports_per_switch:
                break
            slot = port_num // L1_PORTS_PER_SLOT
            port = (port_num % L1_PORTS_PER_SLOT) + 1
            edge_port_name = f"0/{slot}/{port}"
            resp = client.post(
                f"{BASE}/cabling/connections",
                json={
                    "device_a_id": dut_id,
                    "port_a": f"eth{port_idx + 1}",
                    "device_b_id": edge_switch_ids[edge_idx],
                    "port_b": edge_port_name,
                    "connection_type": "ethernet",
                },
            )
            if resp.status_code == 201:
                created += 1
            edge_port_counters[edge_idx] += 1

        done = i + 1
        if done % 500 == 0 or done == total_pa:
            print(f"    PA connections: {done}/{total_pa} devices (created={created})")

    total_clients = len(client_device_ids)
    pa_created = created
    print(
        f"  Creating client-to-edge connections ({total_clients} client devices, "
        f"{PORTS_PER_CLIENT} ports each)..."
    )
    for i, dut_id in enumerate(client_device_ids):
        edge_idx = (total_pa + i) % len(edge_switch_ids)
        for port_idx in range(PORTS_PER_CLIENT):
            port_num = edge_port_counters[edge_idx]
            if port_num >= dut_ports_per_switch:
                break
            slot = port_num // L1_PORTS_PER_SLOT
            port = (port_num % L1_PORTS_PER_SLOT) + 1
            edge_port_name = f"0/{slot}/{port}"
            resp = client.post(
                f"{BASE}/cabling/connections",
                json={
                    "device_a_id": dut_id,
                    "port_a": f"eth{port_idx + 1}",
                    "device_b_id": edge_switch_ids[edge_idx],
                    "port_b": edge_port_name,
                    "connection_type": "ethernet",
                },
            )
            if resp.status_code == 201:
                created += 1
            edge_port_counters[edge_idx] += 1

        done = i + 1
        if done % 200 == 0 or done == total_clients:
            print(
                f"    Client connections: {done}/{total_clients} devices "
                f"(created={created - pa_created})"
            )

    # Edge-to-hub: each edge connects to both hubs via slot 7
    print(f"  Creating edge-to-hub connections ({len(edge_switch_ids)} x {len(hub_switch_ids)})...")
    hub_port_counters = [0] * len(hub_switch_ids)
    for edge_id in edge_switch_ids:
        for hub_idx, hub_id in enumerate(hub_switch_ids):
            hub_port = hub_port_counters[hub_idx] + 1
            hub_slot = (hub_port - 1) // L1_PORTS_PER_SLOT
            hub_port_in_slot = ((hub_port - 1) % L1_PORTS_PER_SLOT) + 1
            resp = client.post(
                f"{BASE}/cabling/connections",
                json={
                    "device_a_id": edge_id,
                    "port_a": f"0/7/{hub_idx + 1}",
                    "device_b_id": hub_id,
                    "port_b": f"0/{hub_slot}/{hub_port_in_slot}",
                    "connection_type": "ethernet",
                },
            )
            if resp.status_code == 201:
                created += 1
            hub_port_counters[hub_idx] += 1

    # Hub-to-hub: one connection between the two hubs
    if len(hub_switch_ids) == 2:
        print("  Creating hub-to-hub connection...")
        resp = client.post(
            f"{BASE}/cabling/connections",
            json={
                "device_a_id": hub_switch_ids[0],
                "port_a": "0/7/1",
                "device_b_id": hub_switch_ids[1],
                "port_b": "0/7/1",
                "connection_type": "ethernet",
            },
        )
        if resp.status_code == 201:
            created += 1

    return created


def create_l2_connections(
    client: httpx.Client,
    pa_device_ids: list[str],
    client_device_ids: list[str],
    l2_switch_ids: list[str],
    l2_hub_switch_ids: list[str],
) -> int:
    """Create L2 cabling connections: DUT-to-edge, edge-to-hub, hub-to-hub.

    PA devices use eth6-eth7 (2 ports per device), clients use eth3 (1 port).
    DUTs are round-robin distributed across L2 switches within their lab.
    Edge switches use eth47-eth48 as uplinks to the two hub switches.
    Hub switches connect to each other for full L2 star connectivity.

    Returns total connections created.
    """
    if not l2_switch_ids:
        return 0

    # Re-runnability guard
    resp = client.get(
        f"{BASE}/cabling/connections",
        params={"device_id": l2_switch_ids[0], "limit": 1},
    )
    if resp.status_code == 200:
        data = resp.json()
        total = data.get("total", len(data.get("items", [])))
        if total > 0:
            print("  L2 connections already exist, skipping")
            return 0

    created = 0
    l2_port_counters = [0] * len(l2_switch_ids)

    # PA devices: eth6, eth7 to L2 switches
    total_pa = len(pa_device_ids)
    print(
        f"  Creating PA-to-L2 connections ({total_pa} PA devices, {L2_PORTS_PER_PA} ports each)..."
    )
    for i, dut_id in enumerate(pa_device_ids):
        l2_idx = i % len(l2_switch_ids)
        for port_idx in range(L2_PORTS_PER_PA):
            l2_port = l2_port_counters[l2_idx] + 1
            if l2_port > L2_DUT_PORTS_MAX:
                break
            resp = client.post(
                f"{BASE}/cabling/connections",
                json={
                    "device_a_id": dut_id,
                    "port_a": f"eth{6 + port_idx}",
                    "device_b_id": l2_switch_ids[l2_idx],
                    "port_b": f"eth{l2_port}",
                    "connection_type": "ethernet",
                },
            )
            if resp.status_code == 201:
                created += 1
            l2_port_counters[l2_idx] += 1

        done = i + 1
        if done % 500 == 0 or done == total_pa:
            print(f"    PA L2 connections: {done}/{total_pa} devices (created={created})")

    # Client devices: eth3 to L2 switches
    total_clients = len(client_device_ids)
    pa_l2_created = created
    print(
        f"  Creating client-to-L2 connections ({total_clients} client devices, "
        f"{L2_PORTS_PER_CLIENT} port each)..."
    )
    for i, dut_id in enumerate(client_device_ids):
        l2_idx = (total_pa + i) % len(l2_switch_ids)
        for port_idx in range(L2_PORTS_PER_CLIENT):
            l2_port = l2_port_counters[l2_idx] + 1
            if l2_port > L2_DUT_PORTS_MAX:
                break
            resp = client.post(
                f"{BASE}/cabling/connections",
                json={
                    "device_a_id": dut_id,
                    "port_a": f"eth{3 + port_idx}",
                    "device_b_id": l2_switch_ids[l2_idx],
                    "port_b": f"eth{l2_port}",
                    "connection_type": "ethernet",
                },
            )
            if resp.status_code == 201:
                created += 1
            l2_port_counters[l2_idx] += 1

        done = i + 1
        if done % 200 == 0 or done == total_clients:
            print(
                f"    Client L2 connections: {done}/{total_clients} devices "
                f"(created={created - pa_l2_created})"
            )

    # Edge-to-hub: each L2 edge connects to both hubs via eth47, eth48
    n_edges = len(l2_switch_ids)
    n_hubs = len(l2_hub_switch_ids)
    print(f"  Creating L2 edge-to-hub connections ({n_edges} x {n_hubs})...")
    l2_hub_port_counters = [0] * len(l2_hub_switch_ids)
    for edge_id in l2_switch_ids:
        for hub_idx, hub_id in enumerate(l2_hub_switch_ids):
            hub_port = l2_hub_port_counters[hub_idx] + 1
            resp = client.post(
                f"{BASE}/cabling/connections",
                json={
                    "device_a_id": edge_id,
                    "port_a": f"eth{47 + hub_idx}",
                    "device_b_id": hub_id,
                    "port_b": f"eth{hub_port}",
                    "connection_type": "ethernet",
                },
            )
            if resp.status_code == 201:
                created += 1
            l2_hub_port_counters[hub_idx] += 1

    # Hub-to-hub: single connection between the two L2 hubs
    if len(l2_hub_switch_ids) == 2:
        print("  Creating L2 hub-to-hub connection...")
        resp = client.post(
            f"{BASE}/cabling/connections",
            json={
                "device_a_id": l2_hub_switch_ids[0],
                "port_a": "eth47",
                "device_b_id": l2_hub_switch_ids[1],
                "port_b": "eth47",
                "connection_type": "ethernet",
            },
        )
        if resp.status_code == 201:
            created += 1

    return created


def list_existing_topology_names(client: httpx.Client) -> set[str]:
    """Return the set of all existing topology names (for idempotency)."""
    items = fetch_all_items(client, f"{BASE}/cabling/topologies")
    return {t["name"] for t in items}


def get_or_create_topology(
    client: httpx.Client,
    name: str,
    canvas_data: dict,
    existing_names: set[str],
    description: str | None = None,
) -> str | None:
    """Create a topology by name if absent, then set its canvas_data.

    Returns the new topology id, or None if it already existed (skipped) or
    creation failed. Mutates existing_names to include freshly created names.
    """
    if name in existing_names:
        print(f"  Exists topology: {name}")
        return None

    resp = client.post(f"{BASE}/cabling/topologies", json={"name": name})
    if resp.status_code != 201:
        print(f"  Failed to create topology {name} ({resp.status_code}): {resp.text}")
        return None
    tid = resp.json()["id"]

    put_body: dict = {"canvas_data": canvas_data}
    if description is not None:
        put_body["description"] = description
    put_resp = client.put(f"{BASE}/cabling/topologies/{tid}", json=put_body)
    if put_resp.status_code != 200:
        print(f"  Created topology {name} but canvas PUT failed ({put_resp.status_code})")
    else:
        print(f"  Created topology: {name}")
    existing_names.add(name)
    return tid


# Device-record fields the frontend DeviceNode reads. Mirrors the DeviceNodeData
# shape so seeded nodes are indistinguishable from hand-built (live-dropped) ones.
CANVAS_DEVICE_FIELDS = (
    "id",
    "name",
    "topology_type",
    "template_name",
    "template_icon",
    "status",
)


def _node_device_payload(device: dict) -> dict:
    """Project a full inventory device record onto the canvas device shape.

    Keeps only the fields DeviceNode renders, falling back to safe defaults for
    any the API omits so the node never carries a null where the component
    expects a value (name span, topology color, status badge).
    """
    payload = {field: device.get(field) for field in CANVAS_DEVICE_FIELDS}
    payload["id"] = device.get("id")
    payload["name"] = device.get("name") or ""
    payload["topology_type"] = device.get("topology_type") or "PHYSICAL"
    payload["status"] = device.get("status") or "AVAILABLE"
    return payload


def build_canvas(
    device_ids: list[str | None],
    edges: list[tuple[int, int, str]],
    device_lookup: dict[str, dict] | None = None,
) -> dict:
    """Build a React Flow canvas_data dict mirroring the editor output.

    device_ids[i] is the device UUID for node i, or None to emit a node with
    empty data (which forces missing_device on any edge touching it). edges is a
    list of (source_index, target_index, layer) tuples. Node ids are React Flow
    ids ("n0", "n1", ...), distinct from device UUIDs; edges reference node ids.
    Positions are laid out on a deterministic grid so the canvas renders sanely.

    device_lookup maps device UUID to the full inventory record. When present,
    each non-None node gets the full device payload (name, topology_type,
    template_name, status, ...) plus the same label/topologyType the live drop
    path sets, and "type": "deviceNode" so React Flow renders the custom
    DeviceNode instead of its blank default node. A node whose device id is None
    (or missing from the lookup) emits an empty- or thin-data node, still typed
    "deviceNode"; a None slot intentionally forces missing_device on any edge
    touching it.
    """
    device_lookup = device_lookup or {}
    nodes: list[dict] = []
    for i, dev_id in enumerate(device_ids):
        node: dict = {
            "id": f"n{i}",
            "type": "deviceNode",
            "position": {"x": 100 + (i % 4) * 200, "y": 100 + (i // 4) * 150},
        }
        if dev_id is None:
            node["data"] = {}
        else:
            device = device_lookup.get(dev_id)
            if device is None:
                # No record available: keep a thin reference so the load-time
                # hydration in the editor can still fill it in by id.
                node["data"] = {"device": {"id": dev_id}}
            else:
                payload = _node_device_payload(device)
                node["data"] = {
                    "device": payload,
                    "label": payload["name"],
                    "topologyType": payload["topology_type"],
                }
        nodes.append(node)

    edge_list: list[dict] = []
    for j, (source, target, layer) in enumerate(edges):
        edge_list.append(
            {
                "id": f"e{j}",
                "source": f"n{source}",
                "target": f"n{target}",
                "data": {"layer": layer, "isProposal": False},
            }
        )
    return {"nodes": nodes, "edges": edge_list}


def _chain_edges(n: int, layer: str) -> list[tuple[int, int, str]]:
    """Linear chain: 0-1-2-...-(n-1)."""
    return [(i, i + 1, layer) for i in range(n - 1)]


def _star_edges(n: int, layer: str) -> list[tuple[int, int, str]]:
    """Star: node 0 is the hub, all others connect to it."""
    return [(0, i, layer) for i in range(1, n)]


def _ring_edges(n: int, layer: str) -> list[tuple[int, int, str]]:
    """Ring: chain with a wrap-around edge back to node 0."""
    return [(i, (i + 1) % n, layer) for i in range(n)]


def _full_mesh_edges(n: int, layer: str) -> list[tuple[int, int, str]]:
    """Full mesh: every pair of nodes connected once."""
    return [(a, b, layer) for a in range(n) for b in range(a + 1, n)]


def _dual_homed_edges(n: int, layer: str) -> list[tuple[int, int, str]]:
    """Dual-homed: nodes 0,1 are cores; 2,3 are leaves, each to both cores."""
    return [(2, 0, layer), (2, 1, layer), (3, 0, layer), (3, 1, layer)]


# Curated named shapes: (name, node_count, edge_generator, layer). Expanded into
# exactly VALID_TOPOLOGY_TARGET topologies by cycling sets and slicing the device
# pool by a rolling offset, so each variant uses different devices.
SHAPE_CATALOG = [
    ("Dual-Homed Pair", 4, _dual_homed_edges, "L1"),
    ("Linear Chain 3-Node", 3, _chain_edges, "L2"),
    ("Linear Chain 5-Node", 5, _chain_edges, "L2"),
    ("Star - Core Switch", 5, _star_edges, "L1"),
    ("Star - Access Layer", 6, _star_edges, "L2"),
    ("L2 Access Ring", 5, _ring_edges, "L2"),
    ("Spine-Leaf 4-Node", 4, _dual_homed_edges, "L3"),
    ("Three-Tier Web/App/DB", 3, _chain_edges, "L3"),
    ("Firewall Sandwich", 3, _chain_edges, "L3"),
    ("Full Mesh Quad", 4, _full_mesh_edges, "L2"),
    ("Point-to-Point Link", 2, _chain_edges, "L1"),
    ("Hub-and-Spoke 6", 6, _star_edges, "L1"),
    ("Backbone Ring 6", 6, _ring_edges, "L2"),
    ("Collapsed Core Pair", 2, _chain_edges, "L3"),
    ("Edge-to-Hub Uplink", 2, _chain_edges, "L1"),
    ("Leaf Triangle", 3, _ring_edges, "L2"),
    ("Quad Mesh Core", 4, _full_mesh_edges, "L3"),
    ("Two-Tier Distribution", 5, _star_edges, "L2"),
]


def seed_topologies(
    client: httpx.Client,
    edge_switch_ids: list[str],
    hub_switch_ids: list[str],
    l2_switch_ids: list[str],
    l2_hub_switch_ids: list[str],
    isolated_device_ids: list[str],
) -> tuple[int, int]:
    """Seed exactly 50 valid and 10 invalid demo lab topologies.

    Returns (valid_count, invalid_count), counting intended topologies whether
    freshly created or already present, so the summary is stable across re-runs.
    """
    existing_names = list_existing_topology_names(client)

    # Build an id to full-record lookup once, so every canvas node carries the
    # device fields the frontend DeviceNode renders (name, topology_type,
    # template_name, status). The inventory API is the source of truth here, the
    # same data the editor's load-time hydration fetches. Devices missing from
    # the map degrade to a thin {"device": {"id": ...}} node (still hydratable).
    device_lookup: dict[str, dict] = {
        d["id"]: d for d in fetch_all_items(client, f"{BASE}/inventory/devices")
    }

    # Pool of guaranteed-cabled, mutually reachable devices: switch infrastructure
    # ONLY. Every L1/L2 edge and hub switch is deterministically cabled and the
    # whole fabric is one connected component (verified live), so any two switches
    # are mutually reachable. DUTs are deliberately NOT included: the L1 cabling
    # pass exhausts edge-switch ports, so many DUTs (including front-of-list
    # clients) end up uncabled and would make a "valid" topology validate as
    # no_path. 61 switches is ample for 50 topologies of at most 6 nodes.
    cabled_pool = edge_switch_ids + hub_switch_ids + l2_switch_ids + l2_hub_switch_ids
    if len(cabled_pool) < 2:
        print("  Skipping topologies: fewer than 2 cabled devices available")
        return (0, 0)

    pool_len = len(cabled_pool)
    valid_count = 0
    offset = 0
    first_valid_id: str | None = None
    for set_num in range(1, 99):
        if valid_count >= VALID_TOPOLOGY_TARGET:
            break
        for base_name, n, gen, layer in SHAPE_CATALOG:
            if valid_count >= VALID_TOPOLOGY_TARGET:
                break
            name = base_name if set_num == 1 else f"{base_name} (Set {set_num})"
            node_devices: list[str | None] = [
                cabled_pool[(offset + i) % pool_len] for i in range(n)
            ]
            canvas = build_canvas(node_devices, gen(n, layer), device_lookup)
            tid = get_or_create_topology(
                client, name, canvas, existing_names, description="Demo lab topology"
            )
            if first_valid_id is None and tid is not None:
                first_valid_id = tid
            valid_count += 1
            offset = (offset + n) % pool_len

    # Invalid topologies. no_path uses an isolated (uncabled) endpoint;
    # missing_device uses a node with empty data (None slot).
    invalid_specs: list[tuple[str, list[str | None], list[tuple[int, int, str]]]] = []

    def _iso(i: int) -> str:
        return isolated_device_ids[i % len(isolated_device_ids)]

    if isolated_device_ids:
        invalid_specs.extend(
            [
                (
                    "BROKEN - Unreachable Cross-Fabric",
                    [cabled_pool[0], _iso(0)],
                    [(0, 1, "L2")],
                ),
                ("BROKEN - Isolated Leaf", [cabled_pool[1 % pool_len], _iso(1)], [(0, 1, "L1")]),
                ("BROKEN - Orphan Node Link", [_iso(2), _iso(3)], [(0, 1, "L3")]),
                ("BROKEN - Dangling Uplink", [cabled_pool[2 % pool_len], _iso(4)], [(0, 1, "L1")]),
                (
                    "BROKEN - Partial Mesh Gap",
                    [cabled_pool[3 % pool_len], cabled_pool[4 % pool_len], _iso(5)],
                    [(0, 1, "L2"), (1, 2, "L2")],
                ),
                ("BROKEN - Stranded Pair", [_iso(0), _iso(1)], [(0, 1, "L2")]),
            ]
        )
    invalid_specs.extend(
        [
            ("BROKEN - Missing Device Ref", [cabled_pool[0], None], [(0, 1, "L2")]),
            ("BROKEN - Empty Node", [None, cabled_pool[1 % pool_len]], [(0, 1, "L1")]),
            ("BROKEN - Two Empty Nodes", [None, None], [(0, 1, "L3")]),
            (
                "BROKEN - Half-Wired Chain",
                [cabled_pool[2 % pool_len], None, cabled_pool[3 % pool_len]],
                [(0, 1, "L2"), (1, 2, "L2")],
            ),
        ]
    )

    invalid_count = 0
    first_invalid_id: str | None = None
    for name, node_devices, edges in invalid_specs[:INVALID_TOPOLOGY_TARGET]:
        canvas = build_canvas(node_devices, edges, device_lookup)
        tid = get_or_create_topology(
            client, name, canvas, existing_names, description="Deliberately invalid demo topology"
        )
        if first_invalid_id is None and tid is not None:
            first_invalid_id = tid
        invalid_count += 1

    # Best-effort self-check: validate one fresh sample of each kind.
    if first_valid_id and first_invalid_id:
        v = client.post(f"{BASE}/cabling/topologies/{first_valid_id}/validate")
        iv = client.post(f"{BASE}/cabling/topologies/{first_invalid_id}/validate")
        if v.status_code == 200 and iv.status_code == 200:
            print(
                f"  Self-check: sample valid -> valid={v.json()['valid']}, "
                f"sample invalid -> valid={iv.json()['valid']}"
            )

    print(f"  {valid_count} valid, {invalid_count} invalid topologies")
    return (valid_count, invalid_count)


def create_users(client: httpx.Client) -> None:
    total = len(SEED_USERS)
    created = 0
    existed = 0
    failed = 0
    for u in SEED_USERS:
        resp = client.post(
            f"{BASE}/auth/register",
            json={
                "email": u["email"],
                "username": u["username"],
                "password": u["password"],
            },
        )
        if resp.status_code == 201:
            uid = resp.json()["id"]
            created += 1
            if u["role"] == "admin":
                client.put(
                    f"{BASE}/auth/users/{uid}/role",
                    json={"role": "admin"},
                )
        elif resp.status_code == 409:
            existed += 1
        else:
            failed += 1

        done = created + existed + failed
        if done % 100 == 0 or done == total:
            print(
                f"  Users: {done}/{total} (created={created}, existed={existed}, failed={failed})"
            )


# --- ACL test fixtures (dedicated, scoped-visibility demo) -------------------
# A regular user ("scuser") that can see only a single "Santa Clara" device
# group, plus an unscoped admin ("scadmin") that sees everything. Used to
# exercise non-admin device-group ACL filtering by hand. Kept separate from the
# bulk users so it does not entangle with the 1000-user load fixtures.
ACL_TEST_USERS = [
    {"username": "scuser", "email": "scuser@herd.dev", "password": "scuser123", "role": None},
    {"username": "scadmin", "email": "scadmin@herd.dev", "password": "scadmin123", "role": "admin"},
]
SANTA_CLARA_DEVICE_GROUP = "Santa Clara"
SANTA_CLARA_USER_GROUP = "Santa Clara Techs"
SANTA_CLARA_DEVICE_COUNT = 3


def register_acl_user(client: httpx.Client, spec: dict) -> str | None:
    """Idempotently register one user; set admin role if requested. Returns user id."""
    resp = client.post(
        f"{BASE}/auth/register",
        json={
            "email": spec["email"],
            "username": spec["username"],
            "password": spec["password"],
        },
    )
    if resp.status_code == 201:
        uid = resp.json()["id"]
        print(f"  Created user: {spec['username']} ({uid})")
    elif resp.status_code == 409:
        uid = get_user_id(client, spec["username"])
        print(f"  Exists user: {spec['username']} ({uid})")
    else:
        print(f"  Failed to register {spec['username']}: {resp.status_code} {resp.text}")
        return None
    if spec["role"] == "admin" and uid:
        client.put(f"{BASE}/auth/users/{uid}/role", json={"role": "admin"})
    return uid


def pick_dut_template(client: httpx.Client) -> str | None:
    """Pick a device template backed by a Management-connection driver (a DUT).

    The non-admin device list is dut_only: it shows only devices whose driver
    connection_type is Management. A switch-backed template would be filtered out
    of a scoped user's view, so the Santa Clara demo devices must be DUTs. Falls
    back to the first device template if no Management driver is found.
    """
    drivers = client.get(f"{BASE}/inventory/drivers", params={"limit": 500}).json().get("items", [])
    mgmt_driver_ids = {d["id"] for d in drivers if d.get("connection_type") == "Management"}
    listing = client.get(
        f"{BASE}/inventory/templates",
        params={"template_type": "device", "limit": 500},
    )
    items = listing.json().get("items", [])
    for t in items:
        if t.get("driver_id") in mgmt_driver_ids:
            return t["id"]
    return items[0]["id"] if items else None


def seed_acl_test_fixtures(client: httpx.Client) -> None:
    """Stage the Santa Clara ACL demo: scoped user, unscoped admin, one device group."""
    print("\n--- ACL Test Fixtures (Santa Clara) ---")
    template_id = pick_dut_template(client)
    if not template_id:
        print("  No device template available; skipping ACL fixtures")
        return

    # Dedicated devices, grouped only under Santa Clara, so scuser sees exactly these.
    device_ids = []
    for i in range(1, SANTA_CLARA_DEVICE_COUNT + 1):
        name = f"santa-clara-{i:02d}"
        device_ids.append(get_or_create_device(client, name, template_id, ip=f"10.70.0.{i}"))
    print(f"  {len(device_ids)} Santa Clara devices")

    dg_id = get_or_create_device_group(
        client, SANTA_CLARA_DEVICE_GROUP, "Santa Clara lab resources (ACL test)"
    )
    bulk_add_devices_to_group(client, dg_id, device_ids)

    ug_id = get_or_create_group(
        client, SANTA_CLARA_USER_GROUP, "Santa Clara technicians (ACL test)"
    )
    bulk_add_permissions_to_device_group(client, dg_id, [ug_id])

    scuser_id = None
    for spec in ACL_TEST_USERS:
        uid = register_acl_user(client, spec)
        if uid and spec["username"] == "scuser":
            scuser_id = uid
    if scuser_id:
        add_group_member(client, ug_id, scuser_id)
        print(f"  Added scuser to {SANTA_CLARA_USER_GROUP}")
    print(
        "  ACL fixtures ready. Login by email: scuser@herd.dev / scuser123 "
        f"(sees only {SANTA_CLARA_DEVICE_GROUP}); scadmin@herd.dev / scadmin123 (admin, sees all)."
    )


def main() -> None:
    client = httpx.Client(verify=False, timeout=30.0)
    token = login(client)
    client.headers["Authorization"] = f"Bearer {token}"

    # Users
    print("\n--- Users ---")
    create_users(client)

    # Drivers
    print("\n--- Drivers ---")
    management_demo_zip = _make_management_demo_driver_zip()
    network_driver_id = get_or_create_driver(
        client, "Network OS Management", "Management", zip_bytes=management_demo_zip
    )
    endpoint_driver_id = get_or_create_driver(
        client, "Endpoint Management", "Management", zip_bytes=management_demo_zip
    )
    l1_driver_id = get_or_create_driver(
        client,
        "L1 Switch Driver",
        "Layer 1 Switch",
        zip_bytes=_make_l1_driver_zip(),
    )
    l2_driver_id = get_or_create_driver(
        client,
        "L2 Switch Driver",
        "Layer 2 Switch",
        zip_bytes=_make_l2_driver_zip(),
    )
    cisco_6509_driver_id = get_or_create_driver(
        client,
        "Cisco 6509 L2 Driver",
        "Layer 2 Switch",
        zip_bytes=_make_cisco_6509_driver_zip(),
    )

    # Map template names to driver ids
    CLIENT_TEMPLATE_NAMES = {
        "Windows 10 Client",
        "Windows 11 Client",
        "macOS Client",
        "Ubuntu Client",
    }

    # Templates
    print("\n--- Templates ---")
    template_ids: dict[str, str] = {}
    for tmpl in DEVICE_TEMPLATES:
        driver_id = (
            endpoint_driver_id if tmpl["name"] in CLIENT_TEMPLATE_NAMES else network_driver_id
        )
        template_ids[tmpl["name"]] = get_or_create_template(
            client,
            tmpl["name"],
            "device",
            tmpl["description"],
            tmpl["sections"],
            driver_id=driver_id,
            vendor=tmpl["vendor"],
            model=tmpl["model"],
        )

    # L1 switch template (non-exclusive: shared infrastructure)
    l1_template_id = get_or_create_template(
        client,
        "L1-Switch-256",
        "device",
        "Layer 1 switch with 256 backplane ports (8 slots x 32 ports)",
        SECTIONS,
        driver_id=l1_driver_id,
        exclusive=False,
        vendor="Generic",
        model="L1-Switch-256",
    )

    # L2 switch template (non-exclusive: shared infrastructure). No
    # template-level poll cadence: health polling for the screenshot demo is
    # opt-in per-device on a curated subset (see POLL_SUBSET_COUNTS and the L2
    # hub devices enrolled below), so a fresh environment populates badges
    # within minutes instead of polling every L2 edge switch.
    l2_template_id = get_or_create_template(
        client,
        "L2-Switch-48",
        "device",
        "Layer 2 switch with 48 access ports",
        SECTIONS,
        driver_id=l2_driver_id,
        exclusive=False,
        vendor="Generic",
        model="L2-Switch-48",
    )

    # Cisco Catalyst 6509 demo template (non-exclusive: shared infrastructure)
    cisco_6509_template_id = get_or_create_template(  # noqa: F841
        client,
        "Cisco-Catalyst-6509",
        "device",
        "Cisco Catalyst 6509 Layer 2 switch (demo, 48 GigabitEthernet ports on slot 1)",
        SECTIONS,
        driver_id=cisco_6509_driver_id,
        exclusive=False,
        vendor="Cisco",
        model="Catalyst 6509",
    )

    port_template_id = get_or_create_template(
        client, "1Gb Copper", "port", "1Gb copper Ethernet port", PORT_SECTIONS
    )

    # DUT Devices
    print("\n--- DUT Devices ---")
    device_ids: list[str] = []
    pa_device_ids: list[str] = []
    client_device_ids: list[str] = []
    total_devs = len(DEVICES)
    for i, dev in enumerate(DEVICES, 1):
        did = get_or_create_device(
            client,
            dev["name"],
            template_ids[dev["template"]],
            dev["ip"],
            poll_interval_seconds=dev.get("poll_interval_seconds"),
        )
        device_ids.append(did)
        if dev["template"] in CLIENT_TEMPLATE_NAMES:
            client_device_ids.append(did)
        else:
            pa_device_ids.append(did)
        if i % 100 == 0 or i == total_devs:
            print(f"  Devices: {i}/{total_devs}")
    print(f"  PA devices: {len(pa_device_ids)}, client devices: {len(client_device_ids)}")

    # L1 Switch Devices
    print("\n--- L1 Switch Devices ---")
    edge_switch_ids: list[str] = []
    for i in range(1, NUM_EDGE_SWITCHES + 1):
        name = f"L1-Edge-{i:02d}"
        ip = f"172.16.0.{i}"
        did = get_or_create_device(client, name, l1_template_id, ip)
        edge_switch_ids.append(did)
    print(f"  Created {len(edge_switch_ids)} edge switches")

    hub_switch_ids: list[str] = []
    for i in range(1, NUM_HUB_SWITCHES + 1):
        name = f"L1-Hub-{i:02d}"
        ip = f"172.16.1.{i}"
        did = get_or_create_device(
            client, name, l1_template_id, ip, poll_interval_seconds=POLL_INTERVAL_SECONDS
        )
        hub_switch_ids.append(did)
    print(f"  Created {len(hub_switch_ids)} hub switches")

    all_l1_ids = edge_switch_ids + hub_switch_ids

    # L2 Switch Devices
    print("\n--- L2 Switch Devices ---")
    l2_switch_ids: list[str] = []
    for i in range(1, NUM_L2_SWITCHES + 1):
        name = f"L2-Edge-{i:02d}"
        ip = f"172.16.2.{i}"
        did = get_or_create_device(client, name, l2_template_id, ip)
        l2_switch_ids.append(did)
    print(f"  Created {len(l2_switch_ids)} L2 edge switches")

    l2_hub_switch_ids: list[str] = []
    for i in range(1, NUM_L2_HUB_SWITCHES + 1):
        name = f"L2-Hub-{i:02d}"
        ip = f"172.16.3.{i}"
        did = get_or_create_device(
            client, name, l2_template_id, ip, poll_interval_seconds=POLL_INTERVAL_SECONDS
        )
        l2_hub_switch_ids.append(did)
    print(f"  Created {len(l2_hub_switch_ids)} L2 hub switches")

    all_l2_ids = l2_switch_ids + l2_hub_switch_ids

    # Isolated demo devices (zero cabling): known-unreachable endpoints for the
    # invalid-topology demos. They reuse an existing DUT template and are kept out
    # of every ports/cabling/group pass below, so they stay at zero connections.
    print("\n--- Isolated Demo Devices ---")
    isolated_device_ids: list[str] = []
    isolated_template_id = template_ids[DEVICE_TEMPLATES[0]["name"]]
    for i in range(1, NUM_ISOLATED_DEMO_DEVICES + 1):
        name = f"Isolated-Demo-{i:02d}"
        ip = f"{ISOLATED_DEMO_IP_BASE}{i}"
        did = get_or_create_device(client, name, isolated_template_id, ip)
        isolated_device_ids.append(did)
    print(f"  Created {len(isolated_device_ids)} isolated demo devices")

    # DUT Ports (variable count per template type)
    print("\n--- DUT Ports ---")
    for i, (did, dev) in enumerate(zip(device_ids, DEVICES), 1):
        port_count = TEMPLATE_PORT_COUNTS.get(dev["template"], DEFAULT_PORT_COUNT)
        create_ports(client, did, port_template_id, count=port_count, prefix="eth")
        if i % 100 == 0 or i == len(device_ids):
            print(f"  Ports: {i}/{len(device_ids)} devices processed")

    # L1 Switch Ports (256 backplane ports per switch)
    print("\n--- L1 Switch Ports ---")
    for i, sw_id in enumerate(all_l1_ids, 1):
        create_l1_switch_ports(client, sw_id, port_template_id)
        if i % 10 == 0 or i == len(all_l1_ids):
            print(f"  L1 switch ports: {i}/{len(all_l1_ids)} switches processed")

    # L2 Switch Ports (48 ports per switch: edges + hubs)
    print("\n--- L2 Switch Ports ---")
    for i, sw_id in enumerate(all_l2_ids, 1):
        create_ports(client, sw_id, port_template_id, count=L2_PORTS_TOTAL, prefix="eth")
        print(f"  L2 switch ports: {i}/{len(all_l2_ids)} switches processed")

    # Cabling connections (L1)
    print("\n--- L1 Cabling Connections ---")
    num_l1_connections = create_connections(
        client, pa_device_ids, client_device_ids, edge_switch_ids, hub_switch_ids
    )
    print(f"  Total L1 connections created: {num_l1_connections}")

    # Cabling connections (L2)
    print("\n--- L2 Cabling Connections ---")
    num_l2_connections = create_l2_connections(
        client,
        pa_device_ids,
        client_device_ids,
        l2_switch_ids,
        l2_hub_switch_ids,
    )
    print(f"  Total L2 connections created: {num_l2_connections}")
    num_connections = num_l1_connections + num_l2_connections

    # Lab topologies (demo/testing): requires the cabling fabric to exist
    print("\n--- Lab Topologies ---")
    num_valid_topos, num_invalid_topos = seed_topologies(
        client,
        edge_switch_ids,
        hub_switch_ids,
        l2_switch_ids,
        l2_hub_switch_ids,
        isolated_device_ids,
    )

    # Device groups
    print("\n--- Device Groups ---")
    dg_names = ["Lab Alpha", "Lab Bravo", "Lab Charlie"]
    dg_descriptions = [
        "Lab Alpha equipment",
        "Lab Bravo equipment",
        "Lab Charlie equipment",
    ]
    dg_ids = []
    for name, desc in zip(dg_names, dg_descriptions):
        dg_ids.append(get_or_create_device_group(client, name, desc))

    # Split PA devices evenly across 3 device groups
    pa_splits: list[list[str]] = [[], [], []]
    for idx, did in enumerate(pa_device_ids):
        pa_splits[idx % 3].append(did)

    # Split client devices evenly across 3 device groups
    client_splits: list[list[str]] = [[], [], []]
    for idx, did in enumerate(client_device_ids):
        client_splits[idx % 3].append(did)

    # Split L1 edge switches across labs (9 per lab)
    edge_splits: list[list[str]] = [
        edge_switch_ids[0:L1_SWITCHES_PER_LAB],
        edge_switch_ids[L1_SWITCHES_PER_LAB : L1_SWITCHES_PER_LAB * 2],
        edge_switch_ids[L1_SWITCHES_PER_LAB * 2 : L1_SWITCHES_PER_LAB * 3],
    ]

    # Split L2 edge switches across labs (10 per lab)
    l2_splits: list[list[str]] = [
        l2_switch_ids[0:L2_SWITCHES_PER_LAB],
        l2_switch_ids[L2_SWITCHES_PER_LAB : L2_SWITCHES_PER_LAB * 2],
        l2_switch_ids[L2_SWITCHES_PER_LAB * 2 : L2_SWITCHES_PER_LAB * 3],
    ]

    for i, (dg_id, name) in enumerate(zip(dg_ids, dg_names)):
        combined = (
            pa_splits[i]
            + client_splits[i]
            + edge_splits[i]
            + hub_switch_ids
            + l2_splits[i]
            + l2_hub_switch_ids
        )
        pa_n = len(pa_splits[i])
        cl_n = len(client_splits[i])
        l1_n = len(edge_splits[i]) + len(hub_switch_ids)
        l2_n = len(l2_splits[i]) + len(l2_hub_switch_ids)
        print(
            f"  {name}: {pa_n} PA + {cl_n} client + {l1_n} L1 + {l2_n} L2 switches "
            f"= {len(combined)} devices"
        )
        bulk_add_devices_to_group(client, dg_id, combined)

    # User groups
    print("\n--- User Groups ---")
    lab_admins_id = get_or_create_group(client, "Lab Admins", "Lab administration team")
    lab_managers_id = get_or_create_group(client, "Lab Managers", "Lab management team")
    sclab_id = get_or_create_group(client, "Lab Alpha Tier 1", "Lab Alpha tier 1 access")
    plano_id = get_or_create_group(client, "Lab Bravo Tier 1", "Lab Bravo tier 1 access")
    almere_id = get_or_create_group(client, "Lab Charlie Tier 1", "Lab Charlie tier 1 access")
    tier1_groups = [sclab_id, plano_id, almere_id]

    # Add members
    print("\n--- Group Members ---")
    user_map = get_all_user_ids(client)

    # Admins 1-40 in Lab Admins, admins 41-50 in Lab Managers
    lab_admin_count = 0
    for i in range(1, 41):
        uid = user_map.get(f"admin{i}")
        if uid:
            add_group_member(client, lab_admins_id, uid)
            lab_admin_count += 1
    print(f"  Added {lab_admin_count} admins to Lab Admins")

    lab_manager_count = 0
    for i in range(41, NUM_ADMINS + 1):
        uid = user_map.get(f"admin{i}")
        if uid:
            add_group_member(client, lab_managers_id, uid)
            lab_manager_count += 1
    print(f"  Added {lab_manager_count} admins to Lab Managers")

    # Split 1000 users evenly across 3 tier 1 groups (334, 333, 333)
    tier1_counts = [0, 0, 0]
    for i in range(1, NUM_USERS + 1):
        uid = user_map.get(f"user{i}")
        if uid:
            group_idx = (i - 1) % 3
            add_group_member(client, tier1_groups[group_idx], uid)
            tier1_counts[group_idx] += 1
    tier1_names = ["Lab Alpha Tier 1", "Lab Bravo Tier 1", "Lab Charlie Tier 1"]
    for name, count in zip(tier1_names, tier1_counts):
        print(f"  Added {count} users to {name}")

    # Remove all assigned users from "Not Grouped" (idempotent for re-runs)
    print("\n--- Remove from Not Grouped ---")
    not_grouped_id = get_or_create_group(
        client,
        "Not Grouped",
        "Default group for unassigned users",
    )
    all_assigned_uids = [
        uid
        for uname, uid in user_map.items()
        if uname.startswith("admin") or uname.startswith("user")
    ]
    print(f"  Removing {len(all_assigned_uids)} users from Not Grouped")
    bulk_remove_from_group(client, not_grouped_id, all_assigned_uids)

    # Remove all assigned devices from "No Pool" (idempotent for re-runs)
    print("\n--- Remove from No Pool ---")
    no_pool_id = get_or_create_device_group(
        client,
        "No Pool",
        "Default group for unassigned devices",
    )
    all_device_ids = device_ids + all_l1_ids + l2_switch_ids
    print(f"  Removing {len(all_device_ids)} devices from No Pool")
    bulk_remove_devices_from_group(client, no_pool_id, all_device_ids)

    # Device group permissions: match user groups to device groups by keyword
    # Admins (Lab Admins, Lab Managers) get no device group permissions (admin role sees all)
    # Tier 1 user groups map to their matching device group
    print("\n--- Device Group Permissions ---")
    keyword_map = {
        "Alpha": sclab_id,
        "Bravo": plano_id,
        "Charlie": almere_id,
    }
    for i, (dg_id, dg_name) in enumerate(zip(dg_ids, dg_names)):
        matched_user_group_ids = []
        for keyword, ug_id in keyword_map.items():
            if keyword.lower() in dg_name.lower():
                matched_user_group_ids.append(ug_id)
        if matched_user_group_ids:
            print(f"  {dg_name}: assigning {len(matched_user_group_ids)} user group(s)")
            bulk_add_permissions_to_device_group(client, dg_id, matched_user_group_ids)
        else:
            print(f"  {dg_name}: no matching user groups")

    # Dedicated ACL test users + Santa Clara device group (scoped-visibility demo)
    seed_acl_test_fixtures(client)

    # FRR live-config demo (opt-in): real netmiko driver + the two slice1 lab routers.
    # Off by default so the standard seed population is unchanged; set SEED_FRR=1 to add it.
    if os.environ.get("SEED_FRR") == "1":
        seed_frr_demo(client)

    total_dut_ports = sum(
        TEMPLATE_PORT_COUNTS.get(d["template"], DEFAULT_PORT_COUNT) for d in DEVICES
    )
    total_l1_ports = NUM_L1_SWITCHES * L1_PORTS_TOTAL
    total_l2_ports = NUM_L2_SWITCHES * L2_PORTS_TOTAL
    print(
        f"\nDone. {NUM_ADMINS + NUM_USERS} users, 4 drivers, "
        f"{len(DEVICE_TEMPLATES) + 4} templates, "
        f"{TOTAL_DEVICES} DUT devices + {NUM_L1_SWITCHES} L1 + {NUM_L2_SWITCHES} L2 switches "
        f"+ {len(isolated_device_ids)} isolated demo devices, "
        f"{total_dut_ports} DUT ports + {total_l1_ports} L1 ports + {total_l2_ports} L2 ports, "
        f"{num_connections} connections, "
        f"3 device groups, 5 user groups, 3 device group permissions, "
        f"{num_valid_topos} valid + {num_invalid_topos} invalid topologies."
    )


if __name__ == "__main__":
    # `--acl-only` stages just the Santa Clara ACL fixtures against an already
    # seeded stack, so they can be (re)applied without the full ~20 min seed.
    if "--acl-only" in sys.argv:
        acl_client = httpx.Client(verify=False, timeout=30.0)
        acl_token = login(acl_client)
        acl_client.headers["Authorization"] = f"Bearer {acl_token}"
        seed_acl_test_fixtures(acl_client)
    else:
        main()
