"""Static pins for the checked-in emulated-gear test lab (infra/nos-test,
docs/NOS_LAB.md). Runs in CI with no lab and no Docker; it only parses the
compose YAML, in the same style as tests/unit/test_compose_ports.py.

These pins exist to prevent two specific incidents from recurring:

- The LDAP-lab project-name hijack (2026-08-26): an exported
  COMPOSE_PROJECT_NAME outranks a compose file's own `name:` unless the
  invocation is pinned with `-p`. The Makefile's NOS_COMPOSE variable pins
  `-p herd-nos-test`, but this file also pins the compose file's OWN
  `name:` so a developer running `docker compose` by hand from
  infra/nos-test/, with no `-p`, still lands in the right project.
- A port-collision incident that broke a live gate run (2026-09-10): this
  lab's published host ports must never collide with any port the dev or
  gate compose files publish.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
NOS_COMPOSE_PATH = REPO_ROOT / "infra" / "nos-test" / "docker-compose.yml"
BASELINE_PATH = REPO_ROOT / "infra" / "nos-test" / "srl" / "baseline.cli"
FRR_DOCKERFILE_PATH = REPO_ROOT / "infra" / "nos-test" / "frr" / "Dockerfile"
DEV_OVERRIDE_PATH = REPO_ROOT / "docker-compose.override.yml"
DEV_COMPOSE_PATHS = [
    REPO_ROOT / "docker-compose.yml",
    DEV_OVERRIDE_PATH,
]

EXPECTED_SRL_PORT = "2223"
EXPECTED_FRR_PORT = "2224"


def _load_nos_compose() -> dict:
    return yaml.safe_load(NOS_COMPOSE_PATH.read_text())


_ENV_DEFAULT_RE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*:-([^}]*)\}")


def _resolve_env_defaults(value: str) -> str:
    """Replace a compose `${VAR:-default}` interpolation with its default,
    so a naive colon-split does not trip over the `:-` inside it (this lab's
    port entries use exactly this form, e.g. "${HERD_TEST_SRL_PORT:-2223}:22")."""
    return _ENV_DEFAULT_RE.sub(lambda m: m.group(1), value)


def _host_port(entry) -> str:
    """Return the published host port from one compose port entry.

    Handles both the short string forms ("2223:22", "127.0.0.1:2223:22",
    "${VAR:-2223}:22") and the long mapping form
    ({target, published, host_ip}); mirrors test_compose_ports.py's
    `_host_binding` helper.
    """
    if isinstance(entry, dict):
        return str(entry.get("published", ""))
    parts = _resolve_env_defaults(str(entry)).split(":")
    if len(parts) == 3:
        return parts[1]
    if len(parts) == 2:
        return parts[0]
    raise AssertionError(f"unexpected compose port entry: {entry!r}")


def _published_host_ports(compose_path: Path) -> set[str]:
    data = yaml.safe_load(compose_path.read_text())
    ports: set[str] = set()
    for service in data.get("services", {}).values():
        for entry in service.get("ports", []) or []:
            ports.add(_host_port(entry))
    return ports


def test_compose_name_is_pinned():
    data = _load_nos_compose()
    assert data.get("name") == "herd-nos-test", (
        "infra/nos-test/docker-compose.yml must pin name: herd-nos-test so a "
        "developer running docker compose by hand (no -p) still lands in the "
        "right project; see the 2026-08-26 LDAP-lab project-name hijack incident."
    )


def test_no_named_volumes():
    data = _load_nos_compose()
    assert not data.get("volumes"), (
        "the NOS test lab must stay stateless (no named volumes) so `down` "
        "discards node state and the next `up` reboots and reseeds cleanly, "
        "mirroring infra/ldap-test/docker-compose.yml"
    )


def test_both_services_declare_a_healthcheck():
    data = _load_nos_compose()
    services = data["services"]
    assert set(services) == {"srl", "frr"}
    for name, service in services.items():
        assert "healthcheck" in service, f"service {name!r} has no healthcheck"
        assert service["healthcheck"].get("test"), f"service {name!r} healthcheck has no test"


def test_published_ports_match_defaults():
    data = _load_nos_compose()
    srl_ports = data["services"]["srl"]["ports"]
    frr_ports = data["services"]["frr"]["ports"]
    assert any(f"HERD_TEST_SRL_PORT:-{EXPECTED_SRL_PORT}" in str(p) for p in srl_ports), srl_ports
    assert any(f"HERD_TEST_FRR_PORT:-{EXPECTED_FRR_PORT}" in str(p) for p in frr_ports), frr_ports


def test_published_ports_do_not_collide_with_dev_or_gate_stack():
    nos_ports = _published_host_ports(NOS_COMPOSE_PATH)
    assert nos_ports == {EXPECTED_SRL_PORT, EXPECTED_FRR_PORT}

    dev_ports: set[str] = set()
    for compose_path in DEV_COMPOSE_PATHS:
        dev_ports |= _published_host_ports(compose_path)

    collision = nos_ports & dev_ports
    assert not collision, (
        f"infra/nos-test/docker-compose.yml publishes host port(s) {collision} that "
        "also appear in docker-compose.yml or docker-compose.override.yml; a real "
        "port collision like this broke a live gate run on 2026-09-10"
    )


def test_srl_baseline_file_exists_and_ends_with_save_startup():
    assert BASELINE_PATH.exists(), f"missing {BASELINE_PATH}"
    lines = [line for line in BASELINE_PATH.read_text().splitlines() if line.strip()]
    assert lines, "baseline.cli is empty"
    assert lines[-1] == "save startup", (
        "the baseline's LAST command must be `save startup`: this is what clears "
        "the factory-fresh [FACTORY] config-prompt tag that breaks netmiko's "
        "nokia_srl.check_config_mode regex, so it must run after every other "
        "baseline command, not before"
    )


def test_srl_baseline_configures_both_vlan_tagging_interfaces():
    lines = BASELINE_PATH.read_text().splitlines()
    assert "set / interface ethernet-1/1 vlan-tagging true" in lines
    assert "set / interface ethernet-1/2 vlan-tagging true" in lines


def _assert_pinned(ref: str, where: str) -> None:
    """An image reference must carry a `@sha256:` digest, which pins it exactly
    regardless of the tag (or lack of one) alongside it; a digest-only reference
    (no tag at all, e.g. `repo@sha256:...`) is fine. Without a digest, a bare
    `:latest` tag or no tag at all (implicit latest) is not; see issue #783."""
    has_digest = "@sha256:" in ref
    assert has_digest, f"{where}: {ref!r} has no digest pin"
    # Strip the digest before inspecting the tag, so a tag@digest reference like
    # "repo:26.7.2-519@sha256:..." is judged on its tag, not the digest suffix.
    name = ref.split("@sha256:", 1)[0]
    assert not name.endswith(":latest"), f"{where}: {ref!r} pins a floating :latest tag"


def test_no_image_under_nos_lab_or_dev_override_selenium_floats_latest_or_untagged():
    # infra/nos-test/docker-compose.yml: the srl service's `image:` key (frr
    # builds from a Dockerfile, checked separately below).
    data = _load_nos_compose()
    _assert_pinned(data["services"]["srl"]["image"], "infra/nos-test/docker-compose.yml srl")

    # infra/nos-test/frr/Dockerfile: the FROM line.
    from_lines = [
        line
        for line in FRR_DOCKERFILE_PATH.read_text().splitlines()
        if line.strip().upper().startswith("FROM ")
    ]
    assert len(from_lines) == 1, from_lines
    from_ref = from_lines[0].split(None, 1)[1].strip()
    _assert_pinned(from_ref, "infra/nos-test/frr/Dockerfile FROM")

    # docker-compose.override.yml: the selenium service's `image:` key.
    override_data = yaml.safe_load(DEV_OVERRIDE_PATH.read_text())
    _assert_pinned(
        override_data["services"]["selenium"]["image"],
        "docker-compose.override.yml selenium",
    )
