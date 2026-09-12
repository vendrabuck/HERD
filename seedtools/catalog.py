"""Static seed catalog: templates, sizing constants, and the generated fleet.

Pure data and pure functions, no HTTP. The module-level SEED_USERS and DEVICES
are generated once at import so every subcommand sees the same fleet.
"""

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

TOTAL_DEVICES = 1000
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
