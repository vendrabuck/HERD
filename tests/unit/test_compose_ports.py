"""Regression pin for issue #708 and the NATS/Postgres loopback-exposure hardening
that followed it: any host port the BASE compose file publishes, other than
Traefik's plain HTTP/HTTPS (80/443), must bind loopback only. `make prod` uses
only this file, so a wide bind here is reachable from the LAN or the open
internet on a host with no firewall in front of it, regardless of whether the
service behind it authenticates. The dev override must not add a second
binding for the Traefik dashboard: compose merges port lists, so a wide entry
there would leave the dev stack binding one container port twice rather than
replacing the loopback bind. Docker's published ports bypass a host firewall
policy, which is why this is pinned in the file rather than left to the
operator.
"""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_COMPOSE = REPO_ROOT / "docker-compose.yml"
DEV_OVERRIDE = REPO_ROOT / "docker-compose.override.yml"

DASHBOARD_PORT = 8080

# Traefik's own plain HTTP/HTTPS listeners are the one deliberately wide
# binding in the base file: the whole point of the gateway is to be reachable.
# Every other published host port must be loopback-only.
WIDE_BY_DESIGN = {("traefik", "80"), ("traefik", "443")}


def _split_host_port_string(entry: str) -> list[str]:
    """Split a short-form compose port string on ':', except inside a
    ``${VAR:-default}`` shell-style default, whose own ':' is not a field
    separator (e.g. "127.0.0.1:${POSTGRES_PORT:-5433}:5432" is 3 fields, not
    4). A naive ``str.split(":")`` misparses that entry.
    """
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    for ch in entry:
        if ch == "{":
            depth += 1
            current.append(ch)
        elif ch == "}":
            depth -= 1
            current.append(ch)
        elif ch == ":" and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return parts


def _all_service_ports(compose_path: Path) -> dict[str, list]:
    data = yaml.safe_load(compose_path.read_text())
    return {
        name: svc.get("ports", [])
        for name, svc in data.get("services", {}).items()
        if svc.get("ports")
    }


def _traefik_ports(compose_path: Path) -> list:
    return _all_service_ports(compose_path).get("traefik", [])


def _host_binding(entry) -> tuple[str | None, str, str]:
    """Return (host_ip, host_port, container_port) for one compose port entry.

    Handles both the short string forms ("80:80", "127.0.0.1:8080:8080",
    "127.0.0.1:${POSTGRES_PORT:-5433}:5432") and the long mapping form
    ({target, published, host_ip}). Port ranges and protocol suffixes are not
    used in this repo and are not handled.
    """
    if isinstance(entry, dict):
        return (
            entry.get("host_ip"),
            str(entry.get("published", "")),
            str(entry["target"]),
        )
    parts = _split_host_port_string(str(entry))
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        return None, parts[0], parts[1]
    raise AssertionError(f"unexpected compose port entry: {entry!r}")


def _dashboard_bindings(compose_path: Path) -> list[tuple[str | None, str, str]]:
    return [
        binding
        for binding in map(_host_binding, _traefik_ports(compose_path))
        if binding[2] == str(DASHBOARD_PORT)
    ]


def test_base_compose_publishes_traefik_dashboard_on_loopback_only():
    bindings = _dashboard_bindings(BASE_COMPOSE)
    assert bindings, "the base compose file no longer publishes the Traefik dashboard"
    for host_ip, host_port, _container in bindings:
        assert host_ip == "127.0.0.1", (
            f"docker-compose.yml publishes the unauthenticated Traefik dashboard as "
            f"{host_ip or '0.0.0.0'}:{host_port}; it must be 127.0.0.1 (issue #708)"
        )


def test_base_compose_still_publishes_traefik_http_and_https():
    """Guard the parser against reading the wrong block: 80 and 443 stay wide."""
    containers = {binding[2] for binding in map(_host_binding, _traefik_ports(BASE_COMPOSE))}
    assert {"80", "443"} <= containers


def test_dev_override_adds_no_second_dashboard_binding():
    """Port lists merge across compose files, so an 8080 entry here would not
    replace the loopback bind; it would stack a second host binding on the
    same container port. Widening for dev needs `ports: !override`, not an
    extra entry (see the comment in the override's traefik block)."""
    assert _dashboard_bindings(DEV_OVERRIDE) == []


def test_base_compose_publishes_every_port_loopback_only_except_traefik_http_https():
    """NATS (4222, 8222) has no broker authentication, and the Postgres port
    (POSTGRES_PORT, default 5433) is reachable with only a database password
    between it and the network; both used to publish on every interface,
    reachable from the LAN or the open internet with no firewall in front of
    the host. Every published host port in the base compose file, other than
    Traefik's plain 80/443, must be loopback-bound; a new service that adds a
    `ports:` entry without binding it to 127.0.0.1 fails this test rather than
    silently widening the stack's exposure.
    """
    services = _all_service_ports(BASE_COMPOSE)
    assert services, "no service in the base compose file publishes any port"
    unchecked_wide = []
    for service_name, ports in services.items():
        for entry in ports:
            host_ip, host_port, container_port = _host_binding(entry)
            if (service_name, container_port) in WIDE_BY_DESIGN:
                continue
            if host_ip != "127.0.0.1":
                unchecked_wide.append(
                    (service_name, host_ip or "0.0.0.0", host_port, container_port)
                )
    assert not unchecked_wide, (
        "docker-compose.yml publishes these host ports on every interface instead "
        f"of loopback only: {unchecked_wide}"
    )


def test_wide_by_design_allowlist_names_ports_traefik_actually_publishes():
    """Guard the allowlist itself: a stale entry (a port Traefik no longer
    publishes) would silently stop being exercised by the test above."""
    traefik_containers = {
        binding[2] for binding in map(_host_binding, _traefik_ports(BASE_COMPOSE))
    }
    for service_name, container_port in WIDE_BY_DESIGN:
        assert service_name == "traefik", WIDE_BY_DESIGN
        assert container_port in traefik_containers, (
            f"WIDE_BY_DESIGN names traefik container port {container_port}, which "
            "docker-compose.yml no longer publishes"
        )
