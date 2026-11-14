"""Static pin: who holds the Docker socket, and how the restarter is wired (issue #1326).

ADR 0017 moves Save and Restart's Docker access out of the gateway-routed config
service into the restarter (`infra/restarter/`). Compose merges
`docker-compose.override.yml` into `docker-compose.yml` for the dev, gate, and CI
stacks, and `make prod` reads the base file alone, so every check reads BOTH files:
a clause either file could break is checked in each, and a set (socket holders,
`herd-ctl` members, the services that read `config.json`) is the union over both.

No stack, no Docker: the two files are read as YAML, and the restarter and the config
service's restart client are loaded from their files (both are standard library only).

What is checked:

- Socket holders are exactly {restarter, traefik}. A service holds the socket when a
  volume of it, in either file, short or long syntax, has a source or target that
  contains `docker.sock`, or a source that is `/`, `/run`, `/var/run`, or under
  `/var/run/docker` or `/run/docker` (a parent directory carries the socket with
  it). No service in either file uses `volumes_from` (it would inherit another
  service's mounts).
- The restarter is on no network but `herd-ctl`, has no `network_mode`, no `ports`,
  and no `traefik.*` label, in either file.
- The services that list `herd-ctl` under `networks`, in either file, are exactly
  {config, restarter}. Every file that declares the `herd-ctl` network at the top
  level leaves `internal` unset or true and does not mark it `external`, and the base
  file declares it `internal: true`.
- The restarter's base definition has `read_only: true`, `cap_drop: [ALL]`, and
  `no-new-privileges:true` in `security_opt`, and no `cap_add` or `privileged`; the
  override sets none of `read_only`, `cap_drop`, `cap_add`, `privileged`,
  `security_opt` on it.
- The restarter's `HERD_COMPOSE_PROJECT` is exactly one `${COMPOSE_PROJECT_NAME:?...}`
  substitution in the base file, and the override does not set it.
- A: the services, over both files, that mount the named volume `herd-config`, other
  than `config` (its writer, at `/data/herd-config`), equal the restarter's
  RESTART_SERVICES constant, and each of them mounts it read-only at `/etc/herd`.
- The config service's client reaches `restarter` on the restarter's port, and its
  timeout exceeds the restarter's WORST_CASE_SECONDS.

Limits:

- Only these two compose files are read; a third `-f` file, an `env_file`, and a
  run-time `docker run` are not seen. A socket reached some other way (`DOCKER_HOST`
  pointing at a TCP daemon, a device mount, `privileged`) is not judged.
- A volume source spelled through a variable (`${SOCK}:/x`) is matched as text, so
  one that resolves to the socket is missed; none exists today.
- A network attached through an extension field or an anchor is seen only after YAML
  resolves the anchor, which safe_load does; an `!override` or `!reset` tag is not
  understood and fails the load.
"""

from __future__ import annotations

import copy
import importlib.util
import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_PATH = REPO_ROOT / "docker-compose.yml"
OVERRIDE_PATH = REPO_ROOT / "docker-compose.override.yml"
RESTARTER_PATH = REPO_ROOT / "infra" / "restarter" / "restarter.py"
CONFIG_CLIENT_PATH = REPO_ROOT / "services" / "config" / "app" / "docker_ctl.py"

SOCKET_HOLDERS = {"restarter", "traefik"}
CTL_NETWORK = "herd-ctl"
CTL_MEMBERS = {"config", "restarter"}
CONFIG_VOLUME = "herd-config"
READER_TARGET = "/etc/herd"
WRITER = "config"
HARDENING_KEYS = ("read_only", "cap_drop", "cap_add", "privileged", "security_opt")
_PROJECT_VALUE = re.compile(r"\$\{COMPOSE_PROJECT_NAME:\?[^${}]+\}")
_SOCKET_PARENTS = {"/", "/run", "/var/run"}


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _files() -> tuple[dict, dict]:
    return yaml.safe_load(BASE_PATH.read_text()), yaml.safe_load(OVERRIDE_PATH.read_text())


def _services(data: dict) -> dict:
    return {name: (svc or {}) for name, svc in (data.get("services") or {}).items()}


def _volumes(service: dict) -> list[tuple[str, str, bool]]:
    """(source, target, read_only) for every volume entry, short or long syntax."""
    entries = []
    for volume in service.get("volumes") or []:
        if isinstance(volume, dict):
            entries.append(
                (
                    str(volume.get("source", "")),
                    str(volume.get("target", "")),
                    bool(volume.get("read_only", False)),
                )
            )
        else:
            parts = str(volume).split(":")
            if len(parts) == 1:
                entries.append(("", parts[0], False))
            else:
                mode = parts[2] if len(parts) > 2 else ""
                entries.append((parts[0], parts[1], "ro" in mode.split(",")))
    return entries


def _networks(service: dict) -> set[str]:
    networks = service.get("networks") or []
    return set(networks) if isinstance(networks, (list, dict)) else {str(networks)}


def _labels(service: dict) -> list[str]:
    labels = service.get("labels") or []
    if isinstance(labels, dict):
        return [str(key) for key in labels]
    return [str(label).split("=", 1)[0] for label in labels]


def _holds_socket(service: dict) -> bool:
    for source, target, _ in _volumes(service):
        if "docker.sock" in source or "docker.sock" in target:
            return True
        cleaned = source.rstrip("/") or "/"
        if cleaned in _SOCKET_PARENTS or cleaned.startswith(("/var/run/docker", "/run/docker")):
            return True
    return False


# -- The checks, each returning its problems so the detector tests can feed them --


def socket_problems(base: dict, override: dict) -> list[str]:
    problems = []
    holders = set()
    for label, data in (("base", base), ("override", override)):
        for name, service in _services(data).items():
            if _holds_socket(service):
                holders.add(name)
            if service.get("volumes_from"):
                problems.append(f"{label}: {name} uses volumes_from")
    if holders != SOCKET_HOLDERS:
        problems.append(f"socket holders are {sorted(holders)}, expected {sorted(SOCKET_HOLDERS)}")
    return problems


def isolation_problems(base: dict, override: dict) -> list[str]:
    problems = []
    for label, data in (("base", base), ("override", override)):
        restarter = _services(data).get("restarter")
        if restarter is None:
            if label == "base":
                problems.append("base: no restarter service")
            continue
        networks = _networks(restarter)
        if label == "base" and networks != {CTL_NETWORK}:
            problems.append(f"base: restarter networks are {sorted(networks)}")
        if label == "override" and not networks <= {CTL_NETWORK}:
            problems.append(f"override: restarter adds networks {sorted(networks)}")
        for key in ("network_mode", "ports"):
            if key in restarter:
                problems.append(f"{label}: restarter sets {key}")
        traefik = [name for name in _labels(restarter) if name.startswith("traefik.")]
        if traefik:
            problems.append(f"{label}: restarter has traefik labels {traefik}")
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
    for label, data in (("base", base), ("override", override)):
        networks = data.get("networks") or {}
        if CTL_NETWORK not in networks:
            continue
        declared = networks[CTL_NETWORK] or {}
        if declared.get("internal", True) is not True:
            problems.append(f"{label}: {CTL_NETWORK} internal is {declared.get('internal')!r}")
        if declared.get("external"):
            problems.append(f"{label}: {CTL_NETWORK} is external")
    return problems


def hardening_problems(base: dict, override: dict) -> list[str]:
    problems = []
    restarter = _services(base).get("restarter", {})
    if restarter.get("read_only") is not True:
        problems.append("base: restarter read_only is not true")
    if restarter.get("cap_drop") != ["ALL"]:
        problems.append(f"base: restarter cap_drop is {restarter.get('cap_drop')!r}")
    if "no-new-privileges:true" not in (restarter.get("security_opt") or []):
        problems.append("base: restarter lacks no-new-privileges:true")
    for key in ("cap_add", "privileged"):
        if key in restarter:
            problems.append(f"base: restarter sets {key}")
    over = _services(override).get("restarter", {})
    for key in HARDENING_KEYS:
        if key in over:
            problems.append(f"override: restarter sets {key}")
    return problems


def project_problems(base: dict, override: dict) -> list[str]:
    problems = []
    env = _services(base).get("restarter", {}).get("environment") or {}
    value = env.get("HERD_COMPOSE_PROJECT") if isinstance(env, dict) else None
    if not isinstance(value, str) or not _PROJECT_VALUE.fullmatch(value):
        problems.append(f"base: restarter HERD_COMPOSE_PROJECT is {value!r}")
    over_env = _services(override).get("restarter", {}).get("environment") or {}
    over_keys = (
        set(over_env)
        if isinstance(over_env, dict)
        else {str(item).split("=", 1)[0] for item in over_env}
    )
    if "HERD_COMPOSE_PROJECT" in over_keys:
        problems.append("override: restarter sets HERD_COMPOSE_PROJECT")
    return problems


def allowlist_problems(base: dict, override: dict, allowlist: frozenset) -> list[str]:
    problems = []
    readers = set()
    for label, data in (("base", base), ("override", override)):
        for name, service in _services(data).items():
            for source, target, read_only in _volumes(service):
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
    assert client.RESTART_TIMEOUT_SECONDS > restarter_module.WORST_CASE_SECONDS


# -- Detector tests: each check fails on a broken file and passes an honest change --


def _mutated(mutate):
    base, override = copy.deepcopy(_files())
    mutate(base, override)
    return base, override


BROKEN = {
    "socket-back-in-config-override": (
        socket_problems,
        lambda b, o: o["services"]["config"]["volumes"].append(
            "/var/run/docker.sock:/var/run/docker.sock:ro"
        ),
    ),
    "socket-long-syntax-on-auth": (
        socket_problems,
        lambda b, o: b["services"]["auth"]["volumes"].append(
            {"type": "bind", "source": "/var/run/docker.sock", "target": "/s"}
        ),
    ),
    "var-run-parent-on-inventory": (
        socket_problems,
        lambda b, o: b["services"]["inventory"]["volumes"].append("/var/run:/host-run:ro"),
    ),
    "volumes-from-restarter": (
        socket_problems,
        lambda b, o: b["services"]["auth"].__setitem__("volumes_from", ["restarter"]),
    ),
    "restarter-lost-the-socket": (
        socket_problems,
        lambda b, o: b["services"]["restarter"].__setitem__("volumes", []),
    ),
    "restarter-on-herd-net": (
        isolation_problems,
        lambda b, o: b["services"]["restarter"]["networks"].append("herd-net"),
    ),
    "restarter-network-in-override": (
        isolation_problems,
        lambda b, o: (
            o["services"].setdefault("restarter", {}).__setitem__("networks", ["herd-net"])
        ),
    ),
    "restarter-port": (
        isolation_problems,
        lambda b, o: b["services"]["restarter"].__setitem__("ports", ["127.0.0.1:8001:8000"]),
    ),
    "restarter-host-network": (
        isolation_problems,
        lambda b, o: b["services"]["restarter"].__setitem__("network_mode", "host"),
    ),
    "restarter-traefik-label": (
        isolation_problems,
        lambda b, o: b["services"]["restarter"].__setitem__("labels", ["traefik.enable=true"]),
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
        lambda b, o: o.setdefault("networks", {}).__setitem__("herd-ctl", {"internal": False}),
    ),
    "herd-ctl-external": (
        ctl_network_problems,
        lambda b, o: b["networks"]["herd-ctl"].__setitem__("external", True),
    ),
    "restarter-writable-root": (
        hardening_problems,
        lambda b, o: b["services"]["restarter"].pop("read_only"),
    ),
    "restarter-keeps-capabilities": (
        hardening_problems,
        lambda b, o: b["services"]["restarter"].__setitem__("cap_drop", ["NET_RAW"]),
    ),
    "restarter-no-nnp": (
        hardening_problems,
        lambda b, o: b["services"]["restarter"].__setitem__("security_opt", []),
    ),
    "restarter-privileged": (
        hardening_problems,
        lambda b, o: b["services"]["restarter"].__setitem__("privileged", True),
    ),
    "override-cap-add": (
        hardening_problems,
        lambda b, o: (
            o["services"].setdefault("restarter", {}).__setitem__("cap_add", ["SYS_ADMIN"])
        ),
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
    "project-in-override": (
        project_problems,
        lambda b, o: (
            o["services"]
            .setdefault("restarter", {})
            .__setitem__("environment", ["HERD_COMPOSE_PROJECT=other"])
        ),
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
        lambda b, o: b["services"]["auth"]["volumes"].append("herd-config:/data/herd-config"),
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
    "reader-in-long-syntax": lambda b, o: b["services"]["acl"].__setitem__(
        "volumes",
        [{"type": "volume", "source": "herd-config", "target": "/etc/herd", "read_only": True}],
    ),
    "reader-target-with-trailing-slash": lambda b, o: b["services"]["acl"].__setitem__(
        "volumes", ["herd-config:/etc/herd/:ro"]
    ),
    "override-restarter-env-and-healthcheck": lambda b, o: o["services"].__setitem__(
        "restarter", {"environment": {"LOG_LEVEL": "DEBUG"}, "healthcheck": {"interval": "5s"}}
    ),
    "override-redeclares-herd-ctl-empty": lambda b, o: o.setdefault("networks", {}).__setitem__(
        "herd-ctl", None
    ),
    "config-gains-a-volume": lambda b, o: b["services"]["config"]["volumes"].append(
        "./extra:/extra:ro"
    ),
    "networks-as-a-mapping": lambda b, o: b["services"]["restarter"].__setitem__(
        "networks", {"herd-ctl": {"aliases": ["restart"]}}
    ),
    "a-run-volume-elsewhere": lambda b, o: b["services"]["auth"]["volumes"].append(
        "/var/lib/herd:/var/lib/herd:ro"
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
