"""Regression pin for issue #708: the Traefik dashboard and API are served with
``api.insecure: true`` (no authentication), so the BASE compose file, which is
all ``make prod`` uses, must publish port 8080 on loopback only. The dev
override must NOT add a second 8080 binding: compose merges port lists, so a
wide entry there would leave the dev stack binding one container port twice
rather than replacing the loopback bind. Docker's published ports bypass a host
firewall policy, which is why this is pinned in the file rather than left to
the operator.
"""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_COMPOSE = REPO_ROOT / "docker-compose.yml"
DEV_OVERRIDE = REPO_ROOT / "docker-compose.override.yml"

DASHBOARD_PORT = 8080


def _traefik_ports(compose_path: Path) -> list:
    data = yaml.safe_load(compose_path.read_text())
    return data["services"]["traefik"].get("ports", [])


def _host_binding(entry) -> tuple[str | None, str, str]:
    """Return (host_ip, host_port, container_port) for one compose port entry.

    Handles both the short string forms ("80:80", "127.0.0.1:8080:8080") and
    the long mapping form ({target, published, host_ip}). Port ranges and
    protocol suffixes are not used in this repo and are not handled.
    """
    if isinstance(entry, dict):
        return (
            entry.get("host_ip"),
            str(entry.get("published", "")),
            str(entry["target"]),
        )
    parts = str(entry).split(":")
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
