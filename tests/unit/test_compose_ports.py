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

Issue #964 extends the same rule to the dev override: every port
`docker-compose.override.yml` publishes (today only the e2e Selenium Grid on
4444 and its noVNC view on 7900, neither of which authenticates) must bind
loopback too, unless it is listed in DEV_OVERRIDE_WIDE_BY_DESIGN with a reason.
"""

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_COMPOSE = REPO_ROOT / "docker-compose.yml"
DEV_OVERRIDE = REPO_ROOT / "docker-compose.override.yml"

DASHBOARD_PORT = 8080

# Traefik's own plain HTTP/HTTPS listeners are the one deliberately wide
# binding in the base file: the whole point of the gateway is to be reachable.
# Every other published host port must be loopback-only.
WIDE_BY_DESIGN = {("traefik", "80"), ("traefik", "443")}

# The dev override's own allow list of deliberately wide host ports, as
# (service, container port) pairs. It is EMPTY on purpose: the override adds
# no Traefik ports (80/443 come from the base file and are covered above), and
# the only ports it publishes are Selenium's 4444 (an unauthenticated WebDriver
# endpoint) and 7900 (noVNC), both loopback since issue #964. Remote noVNC
# viewing goes through an ssh port-forward, not a wide bind. Adding an entry
# here needs a comment saying why that port must be reachable off the host.
DEV_OVERRIDE_WIDE_BY_DESIGN: set[tuple[str, str]] = set()


class _ComposeLoader(yaml.SafeLoader):
    """SafeLoader that accepts compose's merge tags (`!override`, `!reset`).

    Compose >= 2.24 lets an override write `ports: !override [...]` to replace
    rather than merge a list (the override's traefik comment suggests exactly
    that). Plain `yaml.safe_load` raises on the unknown tag, which would turn
    the loopback check into a crash instead of a check; this loader reads the
    tagged node as its plain value so the replacement list is still checked.
    """


def _construct_merge_tag(loader: yaml.SafeLoader, node: yaml.Node):
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    return loader.construct_scalar(node)


for _tag in ("!override", "!reset"):
    _ComposeLoader.add_constructor(_tag, _construct_merge_tag)


def _load_compose_text(text: str) -> dict:
    return yaml.load(text, Loader=_ComposeLoader) or {}


def _split_host_port_string(entry: str) -> list[str]:
    """Split a short-form compose port string on ':', except inside a
    ``${VAR:-default}`` shell-style default, whose own ':' is not a field
    separator (e.g. "127.0.0.1:${POSTGRES_PORT:-5433}:5432" is 3 fields, not
    4), or inside a bracketed IPv6 host address ("[::1]:4444:4444"). A naive
    ``str.split(":")`` misparses both.
    """
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    for ch in entry:
        if ch in "{[":
            depth += 1
            current.append(ch)
        elif ch in "}]":
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
    return _service_ports_from_text(compose_path.read_text())


def _service_ports_from_text(text: str) -> dict[str, list]:
    data = _load_compose_text(text)
    return {
        name: svc.get("ports", [])
        for name, svc in data.get("services", {}).items()
        if svc.get("ports")
    }


def _traefik_ports(compose_path: Path) -> list:
    return _all_service_ports(compose_path).get("traefik", [])


def _host_binding(entry) -> tuple[str | None, str, str]:
    """Return (host_ip, host_port, container_port) for one compose port entry.

    Handles the short string forms ("80:80", "127.0.0.1:8080:8080",
    "127.0.0.1:${POSTGRES_PORT:-5433}:5432", a bare "4444" or YAML integer,
    which publishes on an ephemeral host port on every interface) and the long
    mapping form ({target, published, host_ip}). A "/tcp" or "/udp" suffix is
    dropped from the container port so allow-list keys stay plain numbers.
    host_ip is returned verbatim, so a variable host address such as
    "${BIND:-127.0.0.1}" is NOT treated as loopback: the check fails closed
    on anything that is not literally 127.0.0.1. Port ranges are not used in
    this repo and are not handled.
    """
    if isinstance(entry, dict):
        return (
            entry.get("host_ip"),
            str(entry.get("published", "")),
            str(entry["target"]).split("/", 1)[0],
        )
    parts = _split_host_port_string(str(entry))
    parts[-1] = parts[-1].split("/", 1)[0]
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        return None, parts[0], parts[1]
    if len(parts) == 1:
        return None, "", parts[0]
    raise AssertionError(f"unexpected compose port entry: {entry!r}")


def _wide_bindings(
    services: dict[str, list], allowed: set[tuple[str, str]]
) -> list[tuple[str, str, str, str]]:
    """Every (service, host_ip, host_port, container_port) that is not bound to
    127.0.0.1 and is not on the given allow list. A missing host_ip reports
    as 0.0.0.0, which is what compose does with it."""
    wide = []
    for service_name, ports in services.items():
        for entry in ports:
            host_ip, host_port, container_port = _host_binding(entry)
            if (service_name, container_port) in allowed:
                continue
            if host_ip != "127.0.0.1":
                wide.append((service_name, host_ip or "0.0.0.0", host_port, container_port))
    return wide


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
    unchecked_wide = _wide_bindings(services, WIDE_BY_DESIGN)
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


def test_dev_override_publishes_every_port_loopback_only():
    """Issue #964: the override is what `make up`, `make test-e2e`, and the
    master/everything gate stack all run with, and it used to publish the e2e
    Selenium Grid (4444, unauthenticated WebDriver sessions inside herd-net)
    and its noVNC view (7900) on every interface. Every service in the
    override is enumerated, not only selenium, so a new wide port anywhere in
    the file fails here unless DEV_OVERRIDE_WIDE_BY_DESIGN names it."""
    services = _all_service_ports(DEV_OVERRIDE)
    unchecked_wide = _wide_bindings(services, DEV_OVERRIDE_WIDE_BY_DESIGN)
    assert not unchecked_wide, (
        "docker-compose.override.yml publishes these host ports on every interface "
        f"instead of loopback only: {unchecked_wide}"
    )


def test_dev_override_still_publishes_selenium_grid_and_novnc():
    """Guard the check above against reading nothing: if the override stopped
    publishing ports (or the parser stopped finding them) it would pass
    vacuously. make test-e2e reaches the Grid from the host on 4444."""
    containers = {
        binding[2]
        for binding in map(_host_binding, _all_service_ports(DEV_OVERRIDE).get("selenium", []))
    }
    assert {"4444", "7900"} <= containers


def test_dev_override_allowlist_names_ports_the_override_actually_publishes():
    """Stale-entry guard for DEV_OVERRIDE_WIDE_BY_DESIGN, the twin of the base
    file's: an entry for a port the override no longer publishes would sit
    there ready to wave a future wide bind through unreviewed."""
    published = {
        (service_name, _host_binding(entry)[2])
        for service_name, ports in _all_service_ports(DEV_OVERRIDE).items()
        for entry in ports
    }
    stale = DEV_OVERRIDE_WIDE_BY_DESIGN - published
    assert not stale, f"DEV_OVERRIDE_WIDE_BY_DESIGN names unpublished ports: {stale}"


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ("80:80", (None, "80", "80")),
        ("127.0.0.1:8080:8080", ("127.0.0.1", "8080", "8080")),
        ("127.0.0.1:${POSTGRES_PORT:-5433}:5432", ("127.0.0.1", "${POSTGRES_PORT:-5433}", "5432")),
        ("${POSTGRES_PORT:-5433}:5432", (None, "${POSTGRES_PORT:-5433}", "5432")),
        ("${E2E_BIND:-127.0.0.1}:7900:7900", ("${E2E_BIND:-127.0.0.1}", "7900", "7900")),
        ("0.0.0.0:4444:4444", ("0.0.0.0", "4444", "4444")),
        ("[::1]:4444:4444", ("[::1]", "4444", "4444")),
        ("127.0.0.1:4444:4444/tcp", ("127.0.0.1", "4444", "4444")),
        ("4444", (None, "", "4444")),
        (4444, (None, "", "4444")),
        ({"target": 4444, "published": 4444}, (None, "4444", "4444")),
        (
            {"target": 7900, "published": "7900", "host_ip": "127.0.0.1", "protocol": "tcp"},
            ("127.0.0.1", "7900", "7900"),
        ),
    ],
)
def test_host_binding_parses_every_compose_port_form(entry, expected):
    assert _host_binding(entry) == expected


def test_host_binding_rejects_an_unrecognised_entry():
    with pytest.raises(AssertionError, match="unexpected compose port entry"):
        _host_binding("1.2.3.4:1:2:3")


@pytest.mark.parametrize(
    "entry",
    [
        "4444:4444",
        "0.0.0.0:4444:4444",
        "4444",
        "${E2E_BIND:-127.0.0.1}:4444:4444",
        "[::]:4444:4444",
        {"target": 4444, "published": 4444},
        {"target": 4444, "published": 4444, "host_ip": "0.0.0.0"},
    ],
)
def test_wide_bindings_flags_every_non_loopback_form(entry):
    """The rule fails closed: anything that is not literally 127.0.0.1,
    including a variable host address whose default happens to be loopback,
    is reported."""
    wide = _wide_bindings({"selenium": [entry]}, set())
    assert len(wide) == 1 and wide[0][0] == "selenium" and wide[0][3] == "4444"


def test_wide_bindings_honours_the_allow_list_by_service_and_container_port():
    services = {"traefik": ["80:80"], "other": ["80:80"]}
    assert _wide_bindings(services, {("traefik", "80")}) == [("other", "0.0.0.0", "80", "80")]


def test_wide_bindings_accepts_loopback_in_short_and_long_form():
    services = {
        "selenium": [
            "127.0.0.1:4444:4444",
            {"target": 7900, "published": 7900, "host_ip": "127.0.0.1"},
        ]
    }
    assert _wide_bindings(services, set()) == []


def test_override_merge_tags_load_and_are_still_checked():
    """`ports: !override [...]` must parse (plain safe_load raises on the tag)
    and the replacement list must still be held to the loopback rule."""
    text = (
        "services:\n"
        "  traefik:\n"
        "    ports: !override\n"
        '      - "8080:8080"\n'
        "  selenium:\n"
        "    environment: !reset {}\n"
        "    ports:\n"
        '      - "127.0.0.1:4444:4444"\n'
    )
    services = _service_ports_from_text(text)
    assert _wide_bindings(services, set()) == [("traefik", "0.0.0.0", "8080", "8080")]
