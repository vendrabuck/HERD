"""Pins for stack isolation on one Docker host (the 2026-10-28 gate incident).

A successful `make everything` leaves the seeded gate stack (compose project
`<dir>-gate`) serving https://localhost on host ports 80 and 443. Two things
went wrong when `make up` was then run beside it:

1. The dev stack could not bind 80/443, so its Traefik stayed Created and its
   services died on name-resolution errors.
2. The gate stack's Traefik discovered the dev stack's containers over the
   shared Docker socket (same `traefik.*` labels, no project constraint) and
   load-balanced across them, half on a network it cannot reach, so the page
   hung.

These tests pin both fixes without a stack and without a Docker daemon:

- `scripts/check_gateway_ports_free.sh` refuses when another project publishes
  host port 80 or 443, run here against a STUB `docker` on PATH;
- `make dev` and `make prod` depend on the guard recipe (run through real `make`
  with the stub, so a refusal provably stops `docker compose up`);
- the compose Traefik command constrains discovery to its own compose project and
  is the only static configuration source.
"""

import os
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_gateway_ports_free.sh"
MAKEFILE = REPO_ROOT / "Makefile"
BASE_COMPOSE = REPO_ROOT / "docker-compose.yml"
DEV_OVERRIDE = REPO_ROOT / "docker-compose.override.yml"
STALE_STATIC_FILE = REPO_ROOT / "infra" / "traefik" / "traefik.yml"

PROJECT_INTERPOLATION = "${COMPOSE_PROJECT_NAME:?"
SEP = "|"


def _stub_docker(tmp_path: Path, holders: dict[str, list[tuple[str, str]]], ps_rc: int = 0):
    """A fake `docker` that answers `docker ps --filter publish=<port>` from
    `holders` (port to [(compose project, container name)]) and records every
    other invocation to calls.log, answering it with exit 0."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    answers = tmp_path / "answers"
    answers.mkdir()
    for port, rows in holders.items():
        (answers / port).write_text("".join(f"{proj}{SEP}{name}\n" for proj, name in rows))
    calls = tmp_path / "calls.log"
    stub = bin_dir / "docker"
    stub.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            if [ "$1" = "ps" ]; then
                echo "$*" >> "{tmp_path}/ps.log"
                [ {ps_rc} -eq 0 ] || exit {ps_rc}
                for arg in "$@"; do
                    case "$arg" in
                        publish=*) port="${{arg#publish=}}" ;;
                    esac
                done
                if [ -f "{answers}/$port" ]; then cat "{answers}/$port"; fi
                exit 0
            fi
            echo "$*" >> "{calls}"
            exit 0
            """
        )
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    env.pop("COMPOSE_PROJECT_NAME", None)
    return env, calls


def _run_script(env, own="herd-public"):
    return subprocess.run(
        ["sh", str(SCRIPT), own], env=env, capture_output=True, text=True, timeout=30
    )


# -- the script ---------------------------------------------------------------


def test_ports_free_exits_zero_silently(tmp_path):
    env, _ = _stub_docker(tmp_path, {})
    result = _run_script(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == ""


def test_both_gateway_ports_are_asked_about_by_host_port(tmp_path):
    env, _ = _stub_docker(tmp_path, {})
    _run_script(env)
    asked = (tmp_path / "ps.log").read_text().splitlines()
    filters = sorted(a.split("--filter ")[1].split()[0] for a in asked)
    assert filters == ["publish=443", "publish=80"], asked
    # The project label is what the script compares, so it must be in the format.
    assert all('{{.Label "com.docker.compose.project"}}' in a for a in asked), asked


def test_gate_stack_on_443_only_refuses_with_both_remedies(tmp_path):
    env, _ = _stub_docker(tmp_path, {"443": [("herd-public-gate", "herd-public-gate-traefik-1")]})
    result = _run_script(env)
    assert result.returncode == 1
    out = result.stdout
    assert "Refusing to start compose project herd-public" in out
    assert "Held by compose project herd-public-gate." in out
    assert "seeded gate stack" in out and "'make everything'" in out
    assert "https://localhost" in out
    assert "use it as is at https://localhost" in out
    assert "run 'make gate-down' (this PURGES the gate stack's database), then 'make up'." in out


def test_gate_stack_on_80_only_also_refuses(tmp_path):
    env, _ = _stub_docker(tmp_path, {"80": [("herd-public-gate", "herd-public-gate-traefik-1")]})
    result = _run_script(env)
    assert result.returncode == 1
    assert "Held by compose project herd-public-gate." in result.stdout


def test_another_checkouts_gate_names_that_checkout(tmp_path):
    env, _ = _stub_docker(tmp_path, {"443": [("herd-other-gate", "herd-other-gate-traefik-1")]})
    result = _run_script(env)
    assert result.returncode == 1
    assert "from the checkout directory named herd-other" in result.stdout
    assert "run 'make gate-down' in that checkout" in result.stdout


def test_non_gate_project_refuses_without_gate_wording(tmp_path):
    rows = [("reportportal", "reportportal-gateway-1")]
    env, _ = _stub_docker(tmp_path, {"80": rows, "443": rows})
    result = _run_script(env)
    assert result.returncode == 1
    out = result.stdout
    assert out.count("Held by compose project reportportal.") == 1, out
    assert "docker compose -p reportportal down" in out
    assert "gate" not in out.replace("make gate-down", "")
    assert "make gate-down" not in out


def test_container_without_compose_project_refuses_by_name(tmp_path):
    env, _ = _stub_docker(tmp_path, {"443": [("", "some-proxy")]})
    result = _run_script(env)
    assert result.returncode == 1
    assert "Held by container some-proxy, which compose did not start." in result.stdout


def test_our_own_project_holding_the_ports_is_allowed(tmp_path):
    rows = [("herd-public", "herd-public-traefik-1")]
    env, _ = _stub_docker(tmp_path, {"80": rows, "443": rows})
    result = _run_script(env)
    assert result.returncode == 0, result.stdout
    assert result.stdout == ""


def test_own_project_does_not_hide_another_holder(tmp_path):
    env, _ = _stub_docker(
        tmp_path,
        {"443": [("herd-public", "herd-public-traefik-1"), ("herd-public-gate", "t")]},
    )
    result = _run_script(env)
    assert result.returncode == 1
    assert "Held by compose project herd-public-gate." in result.stdout


def test_docker_unreachable_is_a_distinct_failure(tmp_path):
    env, _ = _stub_docker(tmp_path, {}, ps_rc=1)
    result = _run_script(env)
    assert result.returncode == 2
    assert "Could not ask Docker" in result.stderr


def test_missing_project_argument_is_a_usage_error(tmp_path):
    env, _ = _stub_docker(tmp_path, {})
    result = subprocess.run(["sh", str(SCRIPT)], env=env, capture_output=True, text=True)
    assert result.returncode == 2
    assert "usage:" in result.stderr


# -- the Makefile wiring ------------------------------------------------------


def _rule(target: str) -> tuple[list[str], list[str]]:
    """(prerequisites, recipe lines) of `target`'s rule, help comment dropped."""
    lines = MAKEFILE.read_text().splitlines()
    for i, line in enumerate(lines):
        if line.startswith(f"{target}:") and not line.startswith(f"{target}::"):
            prereqs = line[len(target) + 1 :].split("##")[0].split()
            recipe = []
            for body in lines[i + 1 :]:
                if not body.startswith("\t"):
                    break
                recipe.append(body)
            return prereqs, recipe
    raise AssertionError(f"no rule for {target}")


def test_up_stays_an_alias_of_dev():
    prereqs, recipe = _rule("up")
    assert prereqs == ["dev"] and recipe == []


@pytest.mark.parametrize("target", ["dev", "prod"])
def test_start_targets_depend_on_the_guard(target):
    prereqs, _ = _rule(target)
    assert "_assert-gateway-ports-free" in prereqs, prereqs


def test_guard_recipe_runs_the_script_for_our_own_project():
    _, recipe = _rule("_assert-gateway-ports-free")
    assert len(recipe) == 1, recipe
    line = recipe[0]
    assert "scripts/check_gateway_ports_free.sh" in line
    assert '"$${COMPOSE_PROJECT_NAME:-$(DEV_PROJECT)}"' in line
    assert "$(MAKE)" not in line and "SUBMAKE" not in line
    assert not line.lstrip("\t").startswith("-"), "a - prefix would ignore the refusal"


def _make(target: str, env) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["make", "-s", "-C", str(REPO_ROOT), target],
        env={**env, "HERD_BUILD": "test", "HERD_BUILD_DATE": ""},
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.skipif(shutil.which("make") is None, reason="make is not installed")
@pytest.mark.parametrize("target", ["up", "dev", "prod"])
def test_refusal_stops_compose_up_through_real_make(tmp_path, target):
    env, calls = _stub_docker(tmp_path, {"443": [("herd-public-gate", "t")]})
    env["COMPOSE_PROJECT_NAME"] = "herd-public"
    result = _make(target, env)
    assert result.returncode != 0
    assert "Held by compose project herd-public-gate." in result.stdout
    assert not calls.exists() or "up" not in calls.read_text().split(), calls.read_text()


@pytest.mark.skipif(shutil.which("make") is None, reason="make is not installed")
@pytest.mark.parametrize("target", ["dev", "prod"])
def test_free_ports_let_compose_up_run_through_real_make(tmp_path, target):
    env, calls = _stub_docker(tmp_path, {})
    env["COMPOSE_PROJECT_NAME"] = "herd-public"
    result = _make(target, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "compose" in calls.read_text() and "up --build -d" in calls.read_text()


@pytest.mark.skipif(shutil.which("make") is None, reason="make is not installed")
def test_logs_hints_at_the_stack_that_is_serving(tmp_path):
    env, calls = _stub_docker(tmp_path, {"443": [("herd-public-gate", "t")]})
    env["COMPOSE_PROJECT_NAME"] = "herd-public"
    result = _make("logs", env)
    assert result.returncode == 0, result.stderr
    assert (
        "compose project herd-public has no containers; https://localhost is served by "
        "compose project herd-public-gate" in result.stdout
    )
    assert "compose logs -f" in calls.read_text()


def test_everything_banner_says_make_up_is_refused_while_the_gate_runs():
    _, recipe = _rule("everything")
    tail = "\n".join(recipe[-4:])
    assert "gate stack left running and seeded at https://localhost" in tail
    assert "'make up' refuses to start the dev stack while it runs" in tail
    assert "make gate-down" in tail and "purge its database" in tail


# -- the compose Traefik definition -------------------------------------------


def _traefik(path: Path) -> dict:
    return yaml.safe_load(path.read_text())["services"].get("traefik") or {}


def _flag(command: list[str], name: str) -> str:
    matches = [c for c in command if c.startswith(f"--{name}=")]
    assert len(matches) == 1, (name, command)
    return matches[0].split("=", 1)[1]


def test_traefik_discovery_is_constrained_to_its_own_compose_project():
    command = _traefik(BASE_COMPOSE)["command"]
    constraint = _flag(command, "providers.docker.constraints")
    assert constraint.startswith("Label(`com.docker.compose.project`,`" + PROJECT_INTERPOLATION)
    assert constraint.endswith("}`)"), constraint


def test_traefik_network_is_its_own_projects_network():
    data = yaml.safe_load(BASE_COMPOSE.read_text())
    network = _flag(data["services"]["traefik"]["command"], "providers.docker.network")
    assert network.startswith(PROJECT_INTERPOLATION) and network.endswith("}_herd-net"), network
    assert "herd-net" in data["networks"]
    assert "herd-net" in data["services"]["traefik"]["networks"]


def test_traefik_has_no_static_configuration_file():
    """Traefik's static sources (file, flags, env) are mutually exclusive and a
    file wins, so a --configFile would silently drop the constraint flag."""
    traefik = _traefik(BASE_COMPOSE)
    assert not any(c.startswith("--configFile") for c in traefik["command"])
    assert not any("traefik.yml" in v for v in traefik.get("volumes", []))
    assert not STALE_STATIC_FILE.exists()


def test_traefik_command_keeps_tls_and_the_https_redirect():
    command = _traefik(BASE_COMPOSE)["command"]
    assert _flag(command, "providers.file.filename") == "/etc/traefik/dynamic.yml"
    assert _flag(command, "providers.docker.exposedByDefault") == "false"
    assert _flag(command, "entryPoints.web.address") == ":80"
    assert _flag(command, "entryPoints.websecure.address") == ":443"
    assert _flag(command, "entryPoints.web.http.redirections.entryPoint.to") == "websecure"


def test_dev_override_does_not_replace_the_traefik_command():
    """An override's `command` REPLACES the base list, constraint included."""
    assert "command" not in _traefik(DEV_OVERRIDE)


def _compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(["docker", "compose", "version"], capture_output=True, timeout=30)
    return probe.returncode == 0


@pytest.mark.skipif(not _compose_available(), reason="docker compose is not installed")
@pytest.mark.parametrize("project", ["herd-public", "herd-public-gate"])
def test_compose_renders_the_project_into_the_traefik_flags(tmp_path, project):
    env = {k: v for k, v in os.environ.items() if k != "COMPOSE_PROJECT_NAME"}
    result = subprocess.run(
        ["docker", "compose", "-f", str(BASE_COMPOSE), "-p", project, "config", "--format", "json"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    command = yaml.safe_load(result.stdout)["services"]["traefik"]["command"]
    assert (
        _flag(command, "providers.docker.constraints")
        == f"Label(`com.docker.compose.project`,`{project}`)"
    )
    assert _flag(command, "providers.docker.network") == f"{project}_herd-net"
