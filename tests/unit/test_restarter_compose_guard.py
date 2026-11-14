"""Static pin: who holds the Docker socket, and how the restarter is wired (issue #1326).

ADR 0017 moves Save and Restart's Docker access out of the gateway-routed config
service into the restarter (`infra/restarter/`). Compose merges
`docker-compose.override.yml` into `docker-compose.yml` for the dev, gate, and CI
stacks, and `make prod` reads the base file alone, so every check reads BOTH files:
a clause either file could break is checked in each, and a set (socket holders,
`herd-ctl` members, the services that read `config.json`) is the union over both.

No stack, no Docker: the two files are read as YAML, and the restarter and the config
service's restart client are loaded from their files (both are standard library only).

Socket paths. The socket is `/var/run/docker.sock`, which is `/run/docker.sock` on a
host where `/var/run` links to `/run`, so both count. A host path reaches the socket
when, after `..` and `.` are resolved and repeated slashes collapsed (a relative path
is taken from the repository root, where both compose files live), it IS one of those
two paths or a directory above one of them, compared component by component:
`/`, `/var`, `/var/run`, and `/run` reach it; `/run/dockerd-metrics` and `/var/lib`
do not.

What is checked:

- Socket holders are exactly {restarter, traefik}, over both files. A service holds
  the socket when one of its mounts has a host path that reaches it: a bind mount
  (short or long syntax), or a named volume whose top-level definition, in either
  file, sets `driver_opts.device` to such a path; or when it uses a top-level
  `configs:` or `secrets:` entry whose `file:` is such a path.
- No top-level `configs:` or `secrets:` entry, used or not, has a `file:` that reaches
  the socket. No service in either file uses `volumes_from` or `extends`, or sets a
  `network_mode` of `service:...` or `container:...` (each would borrow another
  container's mounts, definition, or namespace).
- Traefik's mounts that reach the socket name the socket path itself, not a parent.
- The restarter's base definition mounts exactly one volume, the host's
  `/var/run/docker.sock` at `/var/run/docker.sock`, and the override adds none. In
  neither file does it set `user`, `devices`, `privileged`, `cap_add`, `pid`, `ipc`,
  `network_mode`, `volumes_from`, `extends`, `ports`, `configs`, or `secrets`, or carry
  a `traefik.*` label; its networks are exactly `herd-ctl` in the base file, and the
  override adds no other.
- The services that list `herd-ctl` under `networks`, in either file, are exactly
  {config, restarter}. The base file declares `herd-ctl` with `internal: true`; every
  file that declares it leaves `internal` unset or true and sets neither `external`
  nor `name`; no other network, in either file, has a `name` that is `herd-ctl` or
  ends in `_herd-ctl`.
- The restarter's base definition has `read_only: true`, a `cap_drop` list that
  contains `ALL` in any letter case, and a `security_opt` entry `no-new-privileges`,
  `no-new-privileges:true`, or `no-new-privileges=true`; the override sets none of
  `read_only`, `cap_drop`, `cap_add`, `privileged`, `security_opt` on it.
- The restarter's `HERD_COMPOSE_PROJECT` (mapping or list form) is exactly one
  `${COMPOSE_PROJECT_NAME:?...}` substitution in the base file, and the override does
  not set it.
- A: the services, over both files, that mount the named volume `herd-config`, other
  than `config` (its writer, at `/data/herd-config`), equal the restarter's
  RESTART_SERVICES constant, and each of them mounts it read-only at `/etc/herd`.
- The config service's client reaches `restarter` on the restarter's port, and its
  timeout exceeds the restarter's PASS_DEADLINE_SECONDS.

Limits:

- Only these two compose files are read; a third `-f` file, an `env_file`, and a
  run-time `docker run` are not seen. A socket reached some other way (`DOCKER_HOST`
  pointing at a TCP daemon, a `devices:` entry, `privileged` on a service other than
  the restarter, a symbolic link on the host other than `/var/run` to `/run`) is not
  judged.
- A path spelled through a variable (`${SOCK}:/x`) is read as text, so one that
  resolves to the socket is missed; none exists today.
- Traefik is out of scope by decision (issue #1424): its other mounts and its other
  keys are not judged, only that its socket mount names the socket itself.
- An `!override` or `!reset` YAML tag is not understood and fails the load.
"""

from __future__ import annotations

import copy
import importlib.util
import posixpath
import re
from pathlib import Path, PurePosixPath

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_PATH = REPO_ROOT / "docker-compose.yml"
OVERRIDE_PATH = REPO_ROOT / "docker-compose.override.yml"
RESTARTER_PATH = REPO_ROOT / "infra" / "restarter" / "restarter.py"
CONFIG_CLIENT_PATH = REPO_ROOT / "services" / "config" / "app" / "docker_ctl.py"

SOCKET = "/var/run/docker.sock"
SOCKET_PATHS = (PurePosixPath(SOCKET), PurePosixPath("/run/docker.sock"))
SOCKET_HOLDERS = {"restarter", "traefik"}
CTL_NETWORK = "herd-ctl"
CTL_MEMBERS = {"config", "restarter"}
CONFIG_VOLUME = "herd-config"
READER_TARGET = "/etc/herd"
WRITER = "config"
RESTARTER_FORBIDDEN = (
    "user",
    "devices",
    "privileged",
    "cap_add",
    "pid",
    "ipc",
    "network_mode",
    "volumes_from",
    "extends",
    "ports",
    "configs",
    "secrets",
)
OVERRIDE_HARDENING = ("read_only", "cap_drop", "cap_add", "privileged", "security_opt")
NO_NEW_PRIVILEGES = {"no-new-privileges", "no-new-privileges:true", "no-new-privileges=true"}
_PROJECT_VALUE = re.compile(r"\$\{COMPOSE_PROJECT_NAME:\?[^${}]+\}")
_LABELS = (("base", 0), ("override", 1))


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _files() -> tuple[dict, dict]:
    return yaml.safe_load(BASE_PATH.read_text()), yaml.safe_load(OVERRIDE_PATH.read_text())


def _services(data: dict) -> dict:
    return {name: (svc or {}) for name, svc in (data.get("services") or {}).items()}


def host_path(text: str) -> PurePosixPath | None:
    """The normalized host path a mount source or `file:` names; None for a volume name."""
    text = str(text)
    if text.startswith("/"):
        joined = text
    elif text.startswith("."):
        joined = posixpath.join(str(REPO_ROOT), text)
    else:
        return None
    return PurePosixPath("/" + posixpath.normpath(joined).lstrip("/"))


def reaches_socket(text: str) -> bool:
    """True when the path is a socket path or a directory above one, by component."""
    path = host_path(text)
    if path is None:
        return False
    return any(path == sock or path in sock.parents for sock in SOCKET_PATHS)


def _mounts(service: dict) -> list[tuple[str, str, bool, str]]:
    """(source, target, read_only, kind) per volume entry; kind is bind or volume."""
    entries = []
    for volume in service.get("volumes") or []:
        if isinstance(volume, dict):
            source = str(volume.get("source", ""))
            target = str(volume.get("target", ""))
            read_only = bool(volume.get("read_only", False))
        else:
            parts = str(volume).split(":")
            if len(parts) == 1:
                source, target, read_only = "", parts[0], False
            else:
                mode = parts[2] if len(parts) > 2 else ""
                source, target, read_only = parts[0], parts[1], "ro" in mode.split(",")
        kind = "bind" if host_path(source) is not None else "volume"
        entries.append((source, target, read_only, kind))
    return entries


def _top(base: dict, override: dict, key: str) -> dict:
    merged: dict = {}
    for data in (base, override):
        for name, value in (data.get(key) or {}).items():
            merged.setdefault(name, {}).update(value or {})
    return merged


def _used(service: dict, key: str) -> set[str]:
    used = set()
    for entry in service.get(key) or []:
        used.add(str(entry.get("source")) if isinstance(entry, dict) else str(entry))
    return used


def _networks(service: dict) -> set[str]:
    networks = service.get("networks") or []
    return set(networks) if isinstance(networks, (list, dict)) else {str(networks)}


def _labels(service: dict) -> list[str]:
    labels = service.get("labels") or []
    if isinstance(labels, dict):
        return [str(key) for key in labels]
    return [str(label).split("=", 1)[0] for label in labels]


def _environment(service: dict) -> dict:
    env = service.get("environment") or {}
    if isinstance(env, dict):
        return {str(k): v for k, v in env.items()}
    return {str(item).split("=", 1)[0]: str(item).partition("=")[2] for item in env}


def _socket_mounts(service: dict, volumes: dict) -> list[str]:
    """The host paths through which this service's mounts reach the socket."""
    reached = []
    for source, _, _, kind in _mounts(service):
        if kind == "bind" and reaches_socket(source):
            reached.append(source)
        if kind == "volume":
            device = str(((volumes.get(source) or {}).get("driver_opts") or {}).get("device", ""))
            if reaches_socket(device):
                reached.append(device)
    return reached


# -- The checks, each returning its problems so the detector tests can feed them --


def socket_problems(base: dict, override: dict) -> list[str]:
    problems = []
    volumes = _top(base, override, "volumes")
    file_entries = {
        key: {
            name: str(value.get("file", ""))
            for name, value in _top(base, override, key).items()
            if reaches_socket(str(value.get("file", "")))
        }
        for key in ("configs", "secrets")
    }
    for key, entries in file_entries.items():
        for name, path in entries.items():
            problems.append(f"top-level {key} {name} has file {path}, which reaches the socket")
    holders = set()
    for label, index in _LABELS:
        for name, service in _services((base, override)[index]).items():
            reached = _socket_mounts(service, volumes)
            if reached:
                holders.add(name)
            for key, entries in file_entries.items():
                if _used(service, key) & set(entries):
                    holders.add(name)
            if name == "traefik":
                loose = [p for p in reached if host_path(p) not in SOCKET_PATHS]
                if loose:
                    problems.append(f"{label}: traefik mounts {loose}, not the socket itself")
            for key in ("volumes_from", "extends"):
                if key in service:
                    problems.append(f"{label}: {name} uses {key}")
            mode = str(service.get("network_mode", ""))
            if mode.startswith(("service:", "container:")):
                problems.append(f"{label}: {name} has network_mode {mode}")
    if holders != SOCKET_HOLDERS:
        problems.append(f"socket holders are {sorted(holders)}, expected {sorted(SOCKET_HOLDERS)}")
    return problems


def isolation_problems(base: dict, override: dict) -> list[str]:
    problems = []
    for label, index in _LABELS:
        restarter = _services((base, override)[index]).get("restarter")
        if restarter is None:
            if label == "base":
                problems.append("base: no restarter service")
            continue
        networks = _networks(restarter)
        if label == "base" and networks != {CTL_NETWORK}:
            problems.append(f"base: restarter networks are {sorted(networks)}")
        if label == "override" and not networks <= {CTL_NETWORK}:
            problems.append(f"override: restarter adds networks {sorted(networks)}")
        for key in RESTARTER_FORBIDDEN:
            if key in restarter:
                problems.append(f"{label}: restarter sets {key}")
        traefik = [name for name in _labels(restarter) if name.startswith("traefik.")]
        if traefik:
            problems.append(f"{label}: restarter has traefik labels {traefik}")
        mounts = [(host_path(s), t) for s, t, _, _ in _mounts(restarter)]
        if label == "base" and mounts != [(PurePosixPath(SOCKET), SOCKET)]:
            problems.append(f"base: restarter mounts {restarter.get('volumes')!r}")
        if label == "override" and "volumes" in restarter:
            problems.append("override: restarter sets volumes")
    return problems


def ctl_network_problems(base: dict, override: dict) -> list[str]:
    problems = []
    members = {
        name
        for data in (base, override)
        for name, service in _services(data).items()
        if CTL_NETWORK in _networks(service)
    }
    if members != CTL_MEMBERS:
        problems.append(f"{CTL_NETWORK} members are {sorted(members)}")
    base_net = (base.get("networks") or {}).get(CTL_NETWORK)
    if not isinstance(base_net, dict) or base_net.get("internal") is not True:
        problems.append(f"base: {CTL_NETWORK} is not declared internal: true")
    for label, index in _LABELS:
        networks = (base, override)[index].get("networks") or {}
        for name, declared in networks.items():
            declared = declared or {}
            alias = str(declared.get("name", ""))
            if name != CTL_NETWORK and (alias == CTL_NETWORK or alias.endswith("_" + CTL_NETWORK)):
                problems.append(f"{label}: network {name} is named {alias}")
            if name != CTL_NETWORK:
                continue
            if declared.get("internal", True) is not True:
                problems.append(f"{label}: {CTL_NETWORK} internal is {declared.get('internal')!r}")
            for key in ("external", "name"):
                if key in declared:
                    problems.append(f"{label}: {CTL_NETWORK} sets {key}")
    return problems


def hardening_problems(base: dict, override: dict) -> list[str]:
    problems = []
    restarter = _services(base).get("restarter", {})
    if restarter.get("read_only") is not True:
        problems.append("base: restarter read_only is not true")
    cap_drop = restarter.get("cap_drop") or []
    if not isinstance(cap_drop, list) or "ALL" not in {str(c).upper() for c in cap_drop}:
        problems.append(f"base: restarter cap_drop is {cap_drop!r}")
    if not NO_NEW_PRIVILEGES & {str(o) for o in restarter.get("security_opt") or []}:
        problems.append("base: restarter lacks no-new-privileges")
    over = _services(override).get("restarter", {})
    for key in OVERRIDE_HARDENING:
        if key in over:
            problems.append(f"override: restarter sets {key}")
    return problems


def project_problems(base: dict, override: dict) -> list[str]:
    problems = []
    value = _environment(_services(base).get("restarter", {})).get("HERD_COMPOSE_PROJECT")
    if not isinstance(value, str) or not _PROJECT_VALUE.fullmatch(value):
        problems.append(f"base: restarter HERD_COMPOSE_PROJECT is {value!r}")
    if "HERD_COMPOSE_PROJECT" in _environment(_services(override).get("restarter", {})):
        problems.append("override: restarter sets HERD_COMPOSE_PROJECT")
    return problems


def allowlist_problems(base: dict, override: dict, allowlist: frozenset) -> list[str]:
    problems = []
    readers = set()
    for label, index in _LABELS:
        for name, service in _services((base, override)[index]).items():
            for source, target, read_only, _ in _mounts(service):
                if source != CONFIG_VOLUME or name == WRITER:
                    continue
                readers.add(name)
                if (target.rstrip("/"), read_only) != (READER_TARGET, True):
                    problems.append(
                        f"{label}: {name} mounts {CONFIG_VOLUME} at {target!r}, "
                        f"read_only={read_only}; a reader mounts it read-only at {READER_TARGET}"
                    )
    if readers != set(allowlist):
        problems.append(
            f"services reading {CONFIG_VOLUME} {sorted(readers)} differ from the restarter's "
            f"RESTART_SERVICES {sorted(allowlist)}: "
            f"missing {sorted(readers - allowlist)}, extra {sorted(allowlist - readers)}"
        )
    return problems


# -- The real files --


@pytest.fixture(scope="module")
def files():
    return _files()


@pytest.fixture(scope="module")
def restarter_module():
    return _load_module("herd_restarter_probe", RESTARTER_PATH)


def test_socket_holders_are_exactly_restarter_and_traefik(files):
    assert socket_problems(*files) == []


def test_restarter_is_on_herd_ctl_only_with_no_ports_or_gateway_route(files):
    assert isolation_problems(*files) == []


def test_only_config_and_restarter_join_the_internal_herd_ctl(files):
    assert ctl_network_problems(*files) == []


def test_restarter_runs_read_only_without_capabilities(files):
    assert hardening_problems(*files) == []


def test_restarter_project_is_the_compose_project_name(files):
    assert project_problems(*files) == []


def test_restart_allowlist_equals_the_services_that_read_config_json(files, restarter_module):
    assert allowlist_problems(*files, restarter_module.RESTART_SERVICES) == []


def test_config_client_outlasts_the_restarters_worst_case(files, restarter_module):
    client = _load_module("herd_config_restart_client_probe", CONFIG_CLIENT_PATH)
    assert client.RESTARTER_HOST == "restarter"
    assert "restarter" in _services(files[0])
    assert client.RESTARTER_PORT == restarter_module.LISTEN_PORT
    assert client.RESTART_TIMEOUT_SECONDS > restarter_module.PASS_DEADLINE_SECONDS


@pytest.mark.parametrize(
    ("path", "reaches"),
    [
        ("/var/run/docker.sock", True),
        ("/run/docker.sock", True),
        ("/var/run", True),
        ("/var/run/", True),
        ("/run", True),
        ("/var", True),
        ("/", True),
        ("//var//run", True),
        ("/var/lib/../run", True),
        ("/srv/../var", True),
        ("./" + "../" * 12 + "var/run", True),
        ("/run/dockerd-metrics", False),
        ("/var/run/docker.sock.bak", False),
        ("/var/runner", False),
        ("/var/lib", False),
        ("/run/docker", False),
        ("./services/config/app", False),
        ("herd-config", False),
        ("", False),
    ],
)
def test_reaches_socket_compares_whole_components(path, reaches):
    assert reaches_socket(path) is reaches


# -- Detector tests: each check fails on a broken file and passes an honest change --


def _mutated(mutate):
    base, override = copy.deepcopy(_files())
    mutate(base, override)
    return base, override


def _svc(data: dict, name: str) -> dict:
    services = data.setdefault("services", {})
    if services.get(name) is None:
        services[name] = {}
    return services[name]


def _set(data, name, key, value):
    _svc(data, name)[key] = value


def _add_volume(data, name, volume):
    _svc(data, name).setdefault("volumes", []).append(volume)


def _top_set(data, key, name, value):
    if data.get(key) is None:
        data[key] = {}
    data[key][name] = value


BROKEN = {
    "socket-back-in-config-override": (
        socket_problems,
        lambda b, o: _add_volume(o, "config", "/var/run/docker.sock:/var/run/docker.sock:ro"),
    ),
    "socket-long-syntax-on-auth": (
        socket_problems,
        lambda b, o: _add_volume(
            b, "auth", {"type": "bind", "source": "/var/run/docker.sock", "target": "/s"}
        ),
    ),
    "run-socket-path-on-auth": (
        socket_problems,
        lambda b, o: _add_volume(b, "auth", "/run/docker.sock:/s"),
    ),
    "var-run-parent-on-inventory": (
        socket_problems,
        lambda b, o: _add_volume(b, "inventory", "/var/run:/host-run:ro"),
    ),
    "var-parent-on-inventory": (
        socket_problems,
        lambda b, o: _add_volume(b, "inventory", "/var:/host-var:ro"),
    ),
    "root-on-acl": (
        socket_problems,
        lambda b, o: _add_volume(b, "acl", "/:/host:ro"),
    ),
    "relative-traversal-to-var-run": (
        socket_problems,
        lambda b, o: _add_volume(b, "cabling", "./" + "../" * 12 + "var/run:/r:ro"),
    ),
    "named-volume-binding-var-run": (
        socket_problems,
        lambda b, o: (
            _top_set(
                b,
                "volumes",
                "hostrun",
                {
                    "driver": "local",
                    "driver_opts": {"type": "none", "o": "bind", "device": "/var/run"},
                },
            ),
            _add_volume(b, "secrets", "hostrun:/r"),
        ),
    ),
    "config-file-at-the-socket": (
        socket_problems,
        lambda b, o: (
            _top_set(b, "configs", "sock", {"file": "/var/run/docker.sock"}),
            _set(b, "auth", "configs", ["sock"]),
        ),
    ),
    "secret-file-at-run-unused": (
        socket_problems,
        lambda b, o: _top_set(o, "secrets", "run", {"file": "/run"}),
    ),
    "volumes-from-restarter": (
        socket_problems,
        lambda b, o: _set(b, "auth", "volumes_from", ["restarter"]),
    ),
    "extends-restarter": (
        socket_problems,
        lambda b, o: _set(o, "notifications", "extends", {"service": "restarter"}),
    ),
    "network-mode-service-restarter": (
        socket_problems,
        lambda b, o: _set(b, "auth", "network_mode", "service:restarter"),
    ),
    "network-mode-container": (
        socket_problems,
        lambda b, o: _set(o, "integration", "network_mode", "container:herd-restarter-1"),
    ),
    "traefik-mounts-a-parent": (
        socket_problems,
        lambda b, o: _add_volume(b, "traefik", "/var/run:/var/run:ro"),
    ),
    "restarter-lost-the-socket": (
        socket_problems,
        lambda b, o: _set(b, "restarter", "volumes", []),
    ),
    "restarter-on-herd-net": (
        isolation_problems,
        lambda b, o: b["services"]["restarter"]["networks"].append("herd-net"),
    ),
    "restarter-network-in-override": (
        isolation_problems,
        lambda b, o: _set(o, "restarter", "networks", ["herd-net"]),
    ),
    "restarter-port": (
        isolation_problems,
        lambda b, o: _set(b, "restarter", "ports", ["127.0.0.1:8001:8000"]),
    ),
    "restarter-host-network": (
        isolation_problems,
        lambda b, o: _set(b, "restarter", "network_mode", "host"),
    ),
    "restarter-traefik-label": (
        isolation_problems,
        lambda b, o: _set(b, "restarter", "labels", ["traefik.enable=true"]),
    ),
    "restarter-second-mount": (
        isolation_problems,
        lambda b, o: _add_volume(b, "restarter", "herd-config:/etc/herd:ro"),
    ),
    "restarter-mount-in-override": (
        isolation_problems,
        lambda b, o: _set(o, "restarter", "volumes", ["./x:/x:ro"]),
    ),
    "restarter-socket-at-another-target": (
        isolation_problems,
        lambda b, o: _set(b, "restarter", "volumes", ["/var/run/docker.sock:/docker.sock"]),
    ),
    "restarter-user": (isolation_problems, lambda b, o: _set(b, "restarter", "user", "0:0")),
    "restarter-devices": (
        isolation_problems,
        lambda b, o: _set(o, "restarter", "devices", ["/dev/sda:/dev/sda"]),
    ),
    "restarter-pid": (isolation_problems, lambda b, o: _set(b, "restarter", "pid", "host")),
    "restarter-ipc": (isolation_problems, lambda b, o: _set(o, "restarter", "ipc", "host")),
    "restarter-cap-add": (
        isolation_problems,
        lambda b, o: _set(b, "restarter", "cap_add", ["SYS_ADMIN"]),
    ),
    "restarter-privileged": (
        isolation_problems,
        lambda b, o: _set(b, "restarter", "privileged", True),
    ),
    "restarter-extends": (
        isolation_problems,
        lambda b, o: _set(o, "restarter", "extends", {"service": "auth"}),
    ),
    "auth-joins-herd-ctl": (
        ctl_network_problems,
        lambda b, o: b["services"]["auth"]["networks"].append("herd-ctl"),
    ),
    "config-leaves-herd-ctl": (
        ctl_network_problems,
        lambda b, o: b["services"]["config"]["networks"].remove("herd-ctl"),
    ),
    "herd-ctl-not-internal": (
        ctl_network_problems,
        lambda b, o: b["networks"]["herd-ctl"].pop("internal"),
    ),
    "herd-ctl-internal-false-in-override": (
        ctl_network_problems,
        lambda b, o: _top_set(o, "networks", "herd-ctl", {"internal": False}),
    ),
    "herd-ctl-external": (
        ctl_network_problems,
        lambda b, o: b["networks"]["herd-ctl"].__setitem__("external", True),
    ),
    "herd-ctl-renamed": (
        ctl_network_problems,
        lambda b, o: b["networks"]["herd-ctl"].__setitem__("name", "shared"),
    ),
    "another-network-named-herd-ctl": (
        ctl_network_problems,
        lambda b, o: _top_set(o, "networks", "side", {"name": "herd-ctl"}),
    ),
    "another-network-named-project-herd-ctl": (
        ctl_network_problems,
        lambda b, o: _top_set(b, "networks", "side", {"name": "${COMPOSE_PROJECT_NAME}_herd-ctl"}),
    ),
    "restarter-writable-root": (
        hardening_problems,
        lambda b, o: b["services"]["restarter"].pop("read_only"),
    ),
    "restarter-keeps-capabilities": (
        hardening_problems,
        lambda b, o: _set(b, "restarter", "cap_drop", ["NET_RAW"]),
    ),
    "restarter-no-nnp": (
        hardening_problems,
        lambda b, o: _set(b, "restarter", "security_opt", []),
    ),
    "restarter-nnp-false": (
        hardening_problems,
        lambda b, o: _set(b, "restarter", "security_opt", ["no-new-privileges:false"]),
    ),
    "override-cap-add": (
        hardening_problems,
        lambda b, o: _set(o, "restarter", "cap_add", ["SYS_ADMIN"]),
    ),
    "override-read-only-false": (
        hardening_problems,
        lambda b, o: _set(o, "restarter", "read_only", False),
    ),
    "project-literal": (
        project_problems,
        lambda b, o: b["services"]["restarter"]["environment"].__setitem__(
            "HERD_COMPOSE_PROJECT", "herd"
        ),
    ),
    "project-with-default": (
        project_problems,
        lambda b, o: b["services"]["restarter"]["environment"].__setitem__(
            "HERD_COMPOSE_PROJECT", "${COMPOSE_PROJECT_NAME:-herd}"
        ),
    ),
    "project-literal-in-list-form": (
        project_problems,
        lambda b, o: _set(b, "restarter", "environment", ["HERD_COMPOSE_PROJECT=herd"]),
    ),
    "project-in-override": (
        project_problems,
        lambda b, o: _set(o, "restarter", "environment", ["HERD_COMPOSE_PROJECT=other"]),
    ),
    "new-reader-not-in-allowlist": (
        allowlist_problems,
        lambda b, o: b["services"].__setitem__(
            "newsvc", {"volumes": ["herd-config:/etc/herd:ro"], "networks": ["herd-net"]}
        ),
    ),
    "reader-dropped": (
        allowlist_problems,
        lambda b, o: b["services"]["acl"]["volumes"].remove("herd-config:/etc/herd:ro"),
    ),
    "reader-mounts-read-write": (
        allowlist_problems,
        lambda b, o: b["services"]["acl"]["volumes"].__setitem__(
            b["services"]["acl"]["volumes"].index("herd-config:/etc/herd:ro"),
            "herd-config:/etc/herd",
        ),
    ),
    "second-writer": (
        allowlist_problems,
        lambda b, o: _add_volume(b, "auth", "herd-config:/data/herd-config"),
    ),
}


def _check_with_allowlist(check, base, override):
    if check is allowlist_problems:
        restarter = _load_module("herd_restarter_probe_detector", RESTARTER_PATH)
        return check(base, override, restarter.RESTART_SERVICES)
    return check(base, override)


@pytest.mark.parametrize("case", sorted(BROKEN))
def test_each_check_fails_on_a_broken_file(case):
    check, mutate = BROKEN[case]
    assert _check_with_allowlist(check, *_mutated(mutate)) != []


HONEST = {
    "new-service-on-herd-net": lambda b, o: b["services"].__setitem__(
        "newsvc", {"image": "x:1.2.3", "networks": ["herd-net"]}
    ),
    "reader-in-long-syntax": lambda b, o: _set(
        b,
        "acl",
        "volumes",
        [{"type": "volume", "source": "herd-config", "target": "/etc/herd", "read_only": True}],
    ),
    "reader-target-with-trailing-slash": lambda b, o: _set(
        b, "acl", "volumes", ["herd-config:/etc/herd/:ro"]
    ),
    "override-restarter-env-and-healthcheck": lambda b, o: _set(
        o, "restarter", "healthcheck", {"interval": "5s"}
    ),
    "override-redeclares-herd-ctl-empty": lambda b, o: _top_set(o, "networks", "herd-ctl", None),
    "config-gains-a-volume": lambda b, o: _add_volume(b, "config", "./extra:/extra:ro"),
    "networks-as-a-mapping": lambda b, o: _set(
        b, "restarter", "networks", {"herd-ctl": {"aliases": ["restart"]}}
    ),
    "a-var-lib-volume-elsewhere": lambda b, o: _add_volume(
        b, "auth", "/var/lib/herd:/var/lib/herd:ro"
    ),
    "a-dockerd-metrics-mount": lambda b, o: _add_volume(
        b, "execution", "/run/dockerd-metrics:/metrics:ro"
    ),
    "a-socket-backup-file": lambda b, o: _add_volume(
        b, "execution", "/var/run/docker.sock.bak:/bak:ro"
    ),
    "nnp-short-form": lambda b, o: _set(b, "restarter", "security_opt", ["no-new-privileges"]),
    "nnp-equals-form": lambda b, o: _set(
        b, "restarter", "security_opt", ["no-new-privileges=true", "label=disable"]
    ),
    "cap-drop-lower-case-with-more": lambda b, o: _set(
        b, "restarter", "cap_drop", ["all", "NET_RAW"]
    ),
    "restarter-environment-list-form": lambda b, o: _set(
        b,
        "restarter",
        "environment",
        [
            "HERD_COMPOSE_PROJECT=${COMPOSE_PROJECT_NAME:?the compose project name is empty}",
            "LOG_LEVEL=INFO",
        ],
    ),
    "override-restarter-other-env": lambda b, o: _set(
        o, "restarter", "environment", {"LOG_LEVEL": "DEBUG"}
    ),
    "a-named-volume-with-another-device": lambda b, o: (
        _top_set(b, "volumes", "data", {"driver_opts": {"device": "/srv/herd", "o": "bind"}}),
        _add_volume(b, "auth", "data:/data"),
    ),
    "a-config-from-a-repo-file": lambda b, o: (
        _top_set(b, "configs", "extra", {"file": "./infra/traefik/dynamic.yml"}),
        _set(b, "auth", "configs", ["extra"]),
    ),
    "another-network-with-its-own-name": lambda b, o: _top_set(
        b, "networks", "side", {"name": "herd-side"}
    ),
}


@pytest.mark.parametrize("case", sorted(HONEST))
def test_no_check_fails_on_an_honest_change(case):
    base, override = _mutated(HONEST[case])
    for check in (
        socket_problems,
        isolation_problems,
        ctl_network_problems,
        hardening_problems,
        project_problems,
        allowlist_problems,
    ):
        assert _check_with_allowlist(check, base, override) == [], check.__name__
