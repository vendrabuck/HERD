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
- `make up`, `dev`, `prod`, and the gate's `_master-stack-up` run that guard
  first, with the project name docker compose itself reports for the checkout
  (dev) or the gate project (gate); exercised through real `make` in a scratch
  copy of the checkout, so a refusal provably stops `docker compose up`, with
  DOCKER_HOST pointed at a dead socket so a call that bypassed the stub could
  not reach a real daemon;
- `make restart` recreates a Traefik that still carries the removed
  `traefik.yml` mount before restarting (restarting it would fail to start);
- the compose Traefik command constrains discovery to its own compose project
  and is the only static configuration source;
- the gateway address follows HERD_GATEWAY_BIND (issue #1256): the Makefile
  derives GATEWAY_HOST from the environment or `.env` the way compose does,
  `make gateway-url` prints it, and the guard's remedies, the `make logs` note,
  the `make everything` banner, the gate's health wait, and the load test's
  host all name it, proven through real `make` with a scratch `.env`.
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
CERTS_SCRIPT = REPO_ROOT / "scripts" / "make_certs.sh"
MAKEFILE = REPO_ROOT / "Makefile"
BASE_COMPOSE = REPO_ROOT / "docker-compose.yml"
DEV_OVERRIDE = REPO_ROOT / "docker-compose.override.yml"
STALE_STATIC_FILE = REPO_ROOT / "infra" / "traefik" / "traefik.yml"

PROJECT_INTERPOLATION = "${COMPOSE_PROJECT_NAME:?"
SEP = "|"
DEAD_DOCKER_HOST = "unix:///nonexistent/herd-test-no-daemon.sock"
REAL_DOCKER = shutil.which("docker")
HAS_MAKE = shutil.which("make") is not None

# Every variable that steers the derived gateway address. A shell (or a parent
# `make test-root`, which exports the last two) must not leak them into a test.
GATEWAY_ENV = ("HERD_GATEWAY_BIND", "HERD_GATEWAY_HOST", "HERD_BASE_URL", "E2E_HOST_BASE_URL")
BOUND = "192.0.2.10"  # TEST-NET-1, never a real host


def _compose_available() -> bool:
    if REAL_DOCKER is None:
        return False
    probe = subprocess.run([REAL_DOCKER, "compose", "version"], capture_output=True, timeout=30)
    return probe.returncode == 0


HAS_COMPOSE = _compose_available()


def _stub_docker(
    tmp_path: Path,
    holders: dict[str, list[tuple]],
    *,
    ps_rc: int = 0,
    compose_name: str | None = None,
    forward_compose_config: bool = False,
    traefik_mounts: str | None = None,
):
    """A fake `docker`.

    - `docker ps --filter publish=<port>` answers from `holders` (port to a list
      of (compose project, container name[, working dir])).
    - `docker compose config` prints `name: <compose_name>`, or runs the REAL
      docker compose when `forward_compose_config` (rendering needs no daemon),
      or fails when neither is given.
    - `docker compose ps -q traefik` prints a container id when `traefik_mounts`
      is given, and `docker inspect` prints `traefik_mounts`.
    - Everything else is recorded, one line per call, in calls.log and exits 0.
    """
    stub_dir = tmp_path / "stub"
    bin_dir = stub_dir / "bin"
    bin_dir.mkdir(parents=True)
    answers = stub_dir / "answers"
    answers.mkdir()
    for port, rows in holders.items():
        lines = []
        for row in rows:
            proj, name, workdir = (*row, "")[:3]
            lines.append(f"{proj}{SEP}{name}{SEP}{workdir}\n")
        (answers / port).write_text("".join(lines))
    if compose_name is not None:
        (stub_dir / "compose_name").write_text(compose_name)
    if traefik_mounts is not None:
        (stub_dir / "mounts").write_text(traefik_mounts)
    calls = stub_dir / "calls.log"
    forward = f'exec "{REAL_DOCKER}" "$@"' if forward_compose_config else "exit 1"
    stub = bin_dir / "docker"
    stub.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            d="{stub_dir}"
            if [ "$1" = "ps" ]; then
                echo "$*" >> "$d/ps.log"
                [ {ps_rc} -eq 0 ] || exit {ps_rc}
                for arg in "$@"; do
                    case "$arg" in
                        publish=*) port="${{arg#publish=}}" ;;
                    esac
                done
                if [ -f "$d/answers/$port" ]; then cat "$d/answers/$port"; fi
                exit 0
            fi
            if [ "$1" = "inspect" ]; then
                echo "$*" >> "$d/calls.log"
                cat "$d/mounts"
                exit 0
            fi
            if [ "$1" = "compose" ]; then
                case " $* " in
                    *" config "*)
                        if [ -f "$d/compose_name" ]; then
                            echo "name: $(cat "$d/compose_name")"
                            echo "services: {{}}"
                            exit 0
                        fi
                        {forward}
                        ;;
                    *" ps -q traefik "*)
                        if [ -f "$d/mounts" ]; then echo "cafe0001"; fi
                        exit 0
                        ;;
                    *" ps -aq "*)
                        exit 0
                        ;;
                esac
            fi
            echo "$*" >> "$d/calls.log"
            exit 0
            """
        )
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    for name in (
        "COMPOSE_PROJECT_NAME",
        "HERD_GATE_PROJECT",
        "MAKEFLAGS",
        "MFLAGS",
        *GATEWAY_ENV,
    ):
        env.pop(name, None)
    env["DOCKER_HOST"] = DEAD_DOCKER_HOST
    return env, calls


def _calls(calls: Path) -> list[str]:
    return calls.read_text().splitlines() if calls.exists() else []


def _run_script(env, *args, cwd=None):
    return subprocess.run(
        ["sh", str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        cwd=cwd,
    )


# -- the script ---------------------------------------------------------------


def test_ports_free_exits_zero_silently(tmp_path):
    env, _ = _stub_docker(tmp_path, {})
    result = _run_script(env, "herd-public")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == ""


def test_both_gateway_ports_are_asked_about_by_host_port(tmp_path):
    env, _ = _stub_docker(tmp_path, {})
    _run_script(env, "herd-public")
    asked = (tmp_path / "stub" / "ps.log").read_text().splitlines()
    filters = sorted(a.split("--filter ")[1].split()[0] for a in asked)
    assert filters == ["publish=443", "publish=80"], asked
    # The project label is what the script compares, so it must be in the format.
    assert all('{{.Label "com.docker.compose.project"}}' in a for a in asked), asked


def test_this_checkouts_gate_refuses_with_both_remedies(tmp_path):
    env, _ = _stub_docker(
        tmp_path, {"443": [("herd-public-gate", "herd-public-gate-traefik-1", "/w/HERD-public")]}
    )
    env["HERD_GATE_PROJECT"] = "herd-public-gate"
    result = _run_script(env, "herd-public")
    assert result.returncode == 1
    out = result.stdout
    assert "Refusing to start compose project herd-public" in out
    assert "Held by compose project herd-public-gate (started from /w/HERD-public)." in out
    assert "That is this checkout's seeded gate stack" in out and "'make everything'" in out
    assert "use it as is at https://localhost" in out
    assert (
        "run 'make gate-down' (this PURGES the gate stack's database), then run 'make up'." in out
    )


def test_gate_stack_on_80_only_also_refuses(tmp_path):
    env, _ = _stub_docker(tmp_path, {"80": [("herd-public-gate", "herd-public-gate-traefik-1")]})
    env["HERD_GATE_PROJECT"] = "herd-public-gate"
    result = _run_script(env, "herd-public")
    assert result.returncode == 1
    assert "Held by compose project herd-public-gate." in result.stdout


def test_another_checkouts_gate_gets_the_exact_teardown_command(tmp_path):
    """A gate stack this checkout's `make gate-down` would not purge must not be
    called ours, even when its working directory label points here."""
    env, _ = _stub_docker(tmp_path, {"443": [("herd-other-gate", "t", str(tmp_path))]})
    env["HERD_GATE_PROJECT"] = "herd-public-gate"
    result = _run_script(env, "herd-public", cwd=tmp_path)
    assert result.returncode == 1
    out = result.stdout
    assert f"Held by compose project herd-other-gate (started from {tmp_path})." in out
    assert "'make everything' in another" in out
    assert "this checkout's" not in out
    assert "'docker compose -p herd-other-gate --profile e2e down -v --remove-orphans'" in out
    assert "then run 'make up' here." in out


def test_retry_text_is_the_callers(tmp_path):
    env, _ = _stub_docker(tmp_path, {"443": [("herd-public-gate", "t")]})
    env["HERD_GATE_PROJECT"] = "herd-public-gate"
    result = _run_script(env, "herd-x-gate", "rerun the gate")
    assert result.returncode == 1
    assert "then rerun the gate." in result.stdout
    assert "'make up'" not in result.stdout


def test_gateway_host_names_the_address_in_both_gate_remedies(tmp_path):
    """HERD_GATEWAY_HOST (the Makefile's GATEWAY_HOST) replaces localhost in the
    'serving' and 'use it as is' lines, for this checkout's gate and another's."""
    for holder, own_gate in (("herd-public-gate", "herd-public-gate"), ("herd-x-gate", "")):
        env, _ = _stub_docker(tmp_path / holder, {"443": [(holder, "t")]})
        env["HERD_GATE_PROJECT"] = own_gate
        env["HERD_GATEWAY_HOST"] = BOUND
        result = _run_script(env, "herd-public")
        assert result.returncode == 1
        out = result.stdout
        assert f"serving https://{BOUND}" in out, out
        assert f"use it as is at https://{BOUND}, or" in out, out
        assert "localhost" not in out, out


def test_empty_gateway_host_falls_back_to_localhost(tmp_path):
    env, _ = _stub_docker(tmp_path, {"443": [("herd-public-gate", "t")]})
    env["HERD_GATE_PROJECT"] = "herd-public-gate"
    env["HERD_GATEWAY_HOST"] = ""
    result = _run_script(env, "herd-public")
    assert "use it as is at https://localhost, or" in result.stdout


def test_non_gate_project_refuses_without_gate_wording(tmp_path):
    rows = [("reportportal", "reportportal-gateway-1")]
    env, _ = _stub_docker(tmp_path, {"80": rows, "443": rows})
    result = _run_script(env, "herd-public")
    assert result.returncode == 1
    out = result.stdout
    assert out.count("Held by compose project reportportal.") == 1, out
    assert "docker compose -p reportportal down" in out
    assert "gate" not in out


def test_container_without_compose_project_refuses_by_name(tmp_path):
    env, _ = _stub_docker(tmp_path, {"443": [("", "some-proxy")]})
    result = _run_script(env, "herd-public")
    assert result.returncode == 1
    assert "Held by container some-proxy, which compose did not start." in result.stdout


def test_our_own_project_holding_the_ports_is_allowed(tmp_path):
    rows = [("herd-public", "herd-public-traefik-1")]
    env, _ = _stub_docker(tmp_path, {"80": rows, "443": rows})
    result = _run_script(env, "herd-public")
    assert result.returncode == 0, result.stdout
    assert result.stdout == ""


def test_own_project_does_not_hide_another_holder(tmp_path):
    env, _ = _stub_docker(
        tmp_path,
        {"443": [("herd-public", "herd-public-traefik-1"), ("herd-public-gate", "t")]},
    )
    result = _run_script(env, "herd-public")
    assert result.returncode == 1
    assert "Held by compose project herd-public-gate." in result.stdout


def test_list_mode_prints_only_the_holders_that_are_not_ours(tmp_path):
    env, _ = _stub_docker(
        tmp_path,
        {
            "80": [("herd-public", "a"), ("", "proxy")],
            "443": [("", "proxy"), ("herd-public-gate", "t", "/w")],
        },
    )
    result = _run_script(env, "--list", "herd-public")
    assert result.returncode == 0
    assert result.stdout.splitlines() == ["container proxy", "project herd-public-gate"]


def test_docker_unreachable_is_a_distinct_failure(tmp_path):
    env, _ = _stub_docker(tmp_path, {}, ps_rc=1)
    result = _run_script(env, "herd-public")
    assert result.returncode == 2
    assert "Could not ask Docker" in result.stderr


def test_missing_project_argument_is_a_usage_error(tmp_path):
    env, _ = _stub_docker(tmp_path, {})
    result = _run_script(env)
    assert result.returncode == 2
    assert "usage:" in result.stderr


def test_empty_project_name_fails_closed(tmp_path):
    env, _ = _stub_docker(tmp_path, {})
    result = _run_script(env, "")
    assert result.returncode == 2
    assert "Could not determine this checkout's compose project name" in result.stderr


def test_script_header_states_the_two_port_scope():
    header = SCRIPT.read_text().split("set -u", 1)[0]
    assert "Only host ports 80 and 443 are checked, by design" in header


# -- the Makefile wiring ------------------------------------------------------


def _rule(target: str, text: str | None = None) -> tuple[list[str], list[str]]:
    """(prerequisites, recipe lines) of `target`'s rule, help comment dropped."""
    lines = (text if text is not None else MAKEFILE.read_text()).splitlines()
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


def test_dev_guard_asks_compose_for_the_project_name():
    _, recipe = _rule("_assert-gateway-ports-free")
    text = "\n".join(recipe)
    assert "$$($(COMPOSE_OWN_PROJECT_CMD))" in text
    assert "scripts/check_gateway_ports_free.sh" in text
    assert 'HERD_GATEWAY_HOST="$(GATEWAY_HOST)"' in text
    assert "DEV_PROJECT" not in text
    assert "$(MAKE)" not in text and "SUBMAKE" not in text
    assert not any(line.lstrip("\t").startswith("-") for line in recipe)
    definition = [
        line
        for line in MAKEFILE.read_text().splitlines()
        if line.startswith("COMPOSE_OWN_PROJECT_CMD")
    ]
    assert definition == [
        "COMPOSE_OWN_PROJECT_CMD = docker compose config 2>/dev/null | sed -n 's/^name: //p' "
        "| head -n 1"
    ]


def test_gate_stack_up_depends_on_the_gate_guard():
    prereqs, _ = _rule("_master-stack-up")
    assert prereqs[0] == "_assert-gate-ports-free", prereqs
    _, recipe = _rule("_assert-gate-ports-free")
    assert len(recipe) == 1, recipe
    line = recipe[0]
    assert 'sh scripts/check_gateway_ports_free.sh "$(GATE_PROJECT)"' in line
    assert 'HERD_GATEWAY_HOST="$(GATEWAY_HOST)"' in line
    assert not line.lstrip("\t").startswith("-")
    for word in ("$(MAKE)", "exit", "exec"):
        assert word not in line


def _checkout(
    tmp_path: Path, dirname: str, compose: bool = False, dotenv: str | None = None
) -> Path:
    """A scratch copy of the checkout's Makefile, guard script, and the
    certificate script the start targets also run (issue #1239), with a
    scratch `.env` holding `dotenv` when given."""
    root = tmp_path / dirname
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(MAKEFILE, root / "Makefile")
    shutil.copy2(SCRIPT, root / "scripts" / SCRIPT.name)
    shutil.copy2(CERTS_SCRIPT, root / "scripts" / CERTS_SCRIPT.name)
    if compose:
        (root / "docker-compose.yml").write_text("services:\n  a:\n    image: busybox\n")
    if dotenv is not None:
        (root / ".env").write_text(dotenv)
    return root


def _make(root: Path, target: str, env, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["make", "-s", "-C", str(root), *args, target],
        env={**env, "HERD_BUILD": "test", "HERD_BUILD_DATE": ""},
        capture_output=True,
        text=True,
        timeout=60,
    )


def _ran_up(calls: Path) -> bool:
    return any(" up " in f" {c} " for c in _calls(calls))


@pytest.mark.skipif(not HAS_MAKE, reason="make is not installed")
@pytest.mark.parametrize("target", ["up", "dev", "prod"])
def test_refusal_stops_compose_up_through_real_make(tmp_path, target):
    root = _checkout(tmp_path, "herd-public")
    env, calls = _stub_docker(
        tmp_path, {"443": [("herd-public-gate", "t")]}, compose_name="herd-public"
    )
    result = _make(root, target, env)
    assert result.returncode != 0
    assert "Held by compose project herd-public-gate." in result.stdout
    assert "That is this checkout's seeded gate stack" in result.stdout
    assert not _ran_up(calls), _calls(calls)


@pytest.mark.skipif(not HAS_MAKE, reason="make is not installed")
@pytest.mark.parametrize("target", ["dev", "prod"])
def test_free_ports_let_compose_up_run_through_real_make(tmp_path, target):
    root = _checkout(tmp_path, "herd-public")
    env, calls = _stub_docker(tmp_path, {}, compose_name="herd-public")
    result = _make(root, target, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert any(c.startswith("compose") and "up --build -d" in c for c in _calls(calls))


@pytest.mark.skipif(not HAS_MAKE, reason="make is not installed")
@pytest.mark.parametrize(
    ("dirname", "compose_name"),
    [("HERD_Public", "herd_public"), ("HERD.v2", "herdv2")],
)
def test_own_running_stack_is_allowed_when_compose_name_differs_from_dev_project(
    tmp_path, dirname, compose_name
):
    """DEV_PROJECT maps `_` and `.` to `-`; compose keeps `_` and drops `.`. The
    guard must judge by the name compose reports, or it refuses our own stack."""
    root = _checkout(tmp_path, dirname)
    rows = [(compose_name, f"{compose_name}-traefik-1")]
    env, calls = _stub_docker(tmp_path, {"80": rows, "443": rows}, compose_name=compose_name)
    result = _make(root, "up", env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Refusing" not in result.stdout
    assert _ran_up(calls)


@pytest.mark.skipif(not (HAS_MAKE and HAS_COMPOSE), reason="make or docker compose missing")
@pytest.mark.parametrize(
    ("dirname", "compose_name"),
    [("HERD_Public", "herd_public"), ("HERD.v2", "herdv2")],
)
def test_own_stack_allowed_by_the_name_real_compose_derives(tmp_path, dirname, compose_name):
    """Same, with the name coming from the real `docker compose config` of a
    scratch project directory (rendering needs no daemon)."""
    root = _checkout(tmp_path, dirname, compose=True)
    rows = [(compose_name, f"{compose_name}-traefik-1")]
    env, calls = _stub_docker(tmp_path, {"443": rows}, forward_compose_config=True)
    result = _make(root, "up", env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _ran_up(calls)
    # And a foreign holder is still refused under that name.
    env2, calls2 = _stub_docker(
        tmp_path / "second", {"443": [("herd-public-gate", "t")]}, forward_compose_config=True
    )
    refused = _make(root, "up", env2)
    assert refused.returncode != 0
    assert f"Refusing to start compose project {compose_name}:" in refused.stdout
    assert not _ran_up(calls2)


@pytest.mark.skipif(not HAS_MAKE, reason="make is not installed")
def test_compose_that_cannot_answer_fails_closed_through_real_make(tmp_path):
    root = _checkout(tmp_path, "herd-public")
    env, calls = _stub_docker(tmp_path, {})  # no compose_name, no forward: config fails
    result = _make(root, "up", env)
    assert result.returncode != 0
    assert "Could not determine this checkout's compose project name" in result.stderr
    assert not _ran_up(calls)


@pytest.mark.skipif(not HAS_MAKE, reason="make is not installed")
def test_foreign_stack_stops_the_gate_stack_up_through_real_make(tmp_path):
    root = _checkout(tmp_path, "herd-wt")
    env, calls = _stub_docker(tmp_path, {"443": [("herd-public-gate", "t", "/w/HERD-public")]})
    result = _make(root, "_master-stack-up", env)
    assert result.returncode != 0
    assert "Refusing to start compose project herd-wt-gate:" in result.stdout
    assert "'docker compose -p herd-public-gate --profile e2e down -v --remove-orphans'" in (
        result.stdout
    )
    assert "then rerun the gate here." in result.stdout
    assert not _ran_up(calls), _calls(calls)


@pytest.mark.skipif(not HAS_MAKE, reason="make is not installed")
def test_free_ports_let_the_gate_stack_up_run_through_real_make(tmp_path):
    root = _checkout(tmp_path, "herd-wt")
    env, calls = _stub_docker(tmp_path, {})
    result = _make(root, "_master-stack-up", env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "compose -p herd-wt-gate up -d --build" in _calls(calls)


@pytest.mark.skipif(not HAS_MAKE, reason="make is not installed")
def test_restart_recreates_a_traefik_with_the_removed_mount_first(tmp_path):
    root = _checkout(tmp_path, "herd-public")
    env, calls = _stub_docker(
        tmp_path, {}, traefik_mounts="/etc/traefik/traefik.yml /etc/traefik/certs "
    )
    result = _make(root, "restart", env)
    assert result.returncode == 0, result.stdout + result.stderr
    recorded = [c for c in _calls(calls) if c.startswith("compose")]
    assert recorded == ["compose up -d --no-deps --force-recreate traefik", "compose restart"]


@pytest.mark.skipif(not HAS_MAKE, reason="make is not installed")
@pytest.mark.parametrize("mounts", [None, "/etc/traefik/dynamic.yml /etc/traefik/certs "])
def test_restart_only_restarts_a_current_traefik(tmp_path, mounts):
    root = _checkout(tmp_path, "herd-public")
    env, calls = _stub_docker(tmp_path, {}, traefik_mounts=mounts)
    result = _make(root, "restart", env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert [c for c in _calls(calls) if c.startswith("compose")] == ["compose restart"]


@pytest.mark.skipif(not HAS_MAKE, reason="make is not installed")
@pytest.mark.parametrize(
    "holders",
    [
        {"443": [("", "unlabelled-proxy"), ("herd-public-gate", "t")]},
        {"80": [("herd-public-gate", "t")]},
    ],
)
def test_logs_hints_at_the_stack_that_is_serving(tmp_path, holders):
    root = _checkout(tmp_path, "herd-public")
    env, calls = _stub_docker(tmp_path, holders, compose_name="herd-public")
    result = _make(root, "logs", env)
    assert result.returncode == 0, result.stderr
    assert (
        "compose project herd-public has no containers; https://localhost is served by "
        "compose project herd-public-gate" in result.stdout
    )
    assert "compose logs -f" in _calls(calls)


def test_everything_banner_says_make_up_is_refused_while_the_gate_runs():
    _, recipe = _rule("everything")
    tail = "\n".join(recipe[-4:])
    assert "gate stack left running and seeded at $(GATEWAY_URL)" in tail
    assert "'make up' refuses to start the dev stack while it runs" in tail
    assert "make gate-down" in tail and "purge its database" in tail


# -- the gateway address (issue #1256) ----------------------------------------


def _print_make(root: Path, env, *lines: str, args: tuple[str, ...] = ()) -> str:
    """Run `lines` as the recipe of a scratch target in a second makefile read
    after the checkout's own, so they expand against its real variables."""
    extra = root / "print.mk"
    extra.write_text("_test-print:\n" + "".join(f"\t{line}\n" for line in lines))
    result = subprocess.run(
        ["make", "-s", "-C", str(root), "-f", "Makefile", "-f", "print.mk", *args, "_test-print"],
        env={**env, "HERD_BUILD": "test", "HERD_BUILD_DATE": ""},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


@pytest.mark.skipif(not HAS_MAKE, reason="make is not installed")
@pytest.mark.parametrize(
    ("dotenv", "expected"),
    [
        (None, "https://localhost"),
        ("", "https://localhost"),
        ("HERD_GATEWAY_BIND=\n", "https://localhost"),
        ("HERD_GATEWAY_BIND=0.0.0.0\n", "https://localhost"),
        (f"HERD_GATEWAY_BIND={BOUND}\n", f"https://{BOUND}"),
        (f"POSTGRES_DB=herd\nHERD_GATEWAY_BIND={BOUND}\nCORS_ORIGINS=x\n", f"https://{BOUND}"),
        (f'HERD_GATEWAY_BIND="{BOUND}"\n', f"https://{BOUND}"),
        (f"export HERD_GATEWAY_BIND='{BOUND}'  # wired NIC\n", f"https://{BOUND}"),
        (f"HERD_GATEWAY_BIND=198.51.100.1\nHERD_GATEWAY_BIND={BOUND}\n", f"https://{BOUND}"),
        (f"# HERD_GATEWAY_BIND={BOUND}\n", "https://localhost"),
        (f"OTHER_HERD_GATEWAY_BIND={BOUND}\n", "https://localhost"),
    ],
)
def test_gateway_url_follows_the_dotenv_bind_through_real_make(tmp_path, dotenv, expected):
    """`.env` is read as compose reads it: the last assignment wins, quotes, an
    `export` prefix, and a trailing comment are dropped, a commented-out or
    differently named line is not the knob, and 0.0.0.0 or empty means every
    interface, addressed as localhost."""
    root = _checkout(tmp_path, "herd-public", dotenv=dotenv)
    env, calls = _stub_docker(tmp_path, {})
    result = _make(root, "gateway-url", env)
    assert result.returncode == 0, result.stderr
    assert result.stdout == expected + "\n"
    assert _calls(calls) == [] and not (tmp_path / "stub" / "ps.log").exists()


@pytest.mark.skipif(not HAS_MAKE, reason="make is not installed")
def test_environment_and_command_line_bind_beat_dotenv_as_in_compose(tmp_path):
    """Compose interpolation prefers the shell environment over `.env`; so does
    the Makefile, and a command-line value wins over both."""
    root = _checkout(tmp_path, "herd-public", dotenv=f"HERD_GATEWAY_BIND={BOUND}\n")
    env, _ = _stub_docker(tmp_path, {})
    env["HERD_GATEWAY_BIND"] = "198.51.100.7"
    assert _make(root, "gateway-url", env).stdout == "https://198.51.100.7\n"
    env["HERD_GATEWAY_BIND"] = ""
    assert _make(root, "gateway-url", env).stdout == "https://localhost\n"
    del env["HERD_GATEWAY_BIND"]
    cli = _make(root, "gateway-url", env, "HERD_GATEWAY_BIND=0.0.0.0")
    assert cli.stdout == "https://localhost\n"


@pytest.mark.skipif(not HAS_MAKE, reason="make is not installed")
def test_gateway_url_dry_run_only_prints(tmp_path):
    """`make -n gateway-url` is safe: the recipe is a lone echo with no $(MAKE)."""
    root = _checkout(tmp_path, "herd-public", dotenv=f"HERD_GATEWAY_BIND={BOUND}\n")
    env, calls = _stub_docker(tmp_path, {})
    result = _make(root, "gateway-url", env, "-n")
    assert result.returncode == 0, result.stderr
    assert result.stdout == f'echo "https://{BOUND}"\n'
    _, recipe = _rule("gateway-url")
    assert recipe == ['\t@echo "$(GATEWAY_URL)"']
    assert _calls(calls) == []


@pytest.mark.skipif(not HAS_MAKE, reason="make is not installed")
def test_gateway_url_is_listed_by_the_bare_make_help(tmp_path):
    root = _checkout(tmp_path, "herd-public")
    env, _ = _stub_docker(tmp_path, {})
    result = _make(root, "help", env)
    assert result.returncode == 0, result.stderr
    assert any(line.split()[:1] == ["gateway-url"] for line in result.stdout.splitlines())


@pytest.mark.skipif(not HAS_MAKE, reason="make is not installed")
@pytest.mark.parametrize(
    ("dotenv", "environment", "base_url", "e2e_url"),
    [
        (None, {}, "https://localhost/api", "https://localhost"),
        (f"HERD_GATEWAY_BIND={BOUND}\n", {}, f"https://{BOUND}/api", f"https://{BOUND}"),
        (
            f"HERD_GATEWAY_BIND={BOUND}\n",
            {"HERD_BASE_URL": "https://elsewhere/api", "E2E_HOST_BASE_URL": "https://e2e"},
            "https://elsewhere/api",
            "https://e2e",
        ),
    ],
)
def test_harness_urls_are_exported_to_recipes(tmp_path, dotenv, environment, base_url, e2e_url):
    """Seed, integration, contract, Playwright, and locust read HERD_BASE_URL or
    E2E_HOST_BASE_URL from their environment; the recipe environment carries the
    derived defaults, and a value the caller exported still wins."""
    root = _checkout(tmp_path, "herd-public", dotenv=dotenv)
    env, _ = _stub_docker(tmp_path, {})
    env.update(environment)
    out = _print_make(root, env, '@printf \'%s|%s\\n\' "$$HERD_BASE_URL" "$$E2E_HOST_BASE_URL"')
    assert out == f"{base_url}|{e2e_url}\n"


@pytest.mark.skipif(not HAS_MAKE, reason="make is not installed")
@pytest.mark.parametrize(
    ("base_url", "host"),
    [
        (None, f"https://{BOUND}"),
        ("https://localhost", "https://localhost"),
        ("https://localhost/api", "https://localhost"),
        ("https://localhost/api/", "https://localhost"),
        ("https://203.0.113.5", "https://203.0.113.5"),
    ],
)
def test_load_host_is_the_gateway_root(tmp_path, base_url, host):
    """The locustfile's paths start with /api, so test-load's --host drops a
    trailing /api from HERD_BASE_URL (the exported default has one; nightly.yml
    passes the bare https://localhost)."""
    root = _checkout(tmp_path, "herd-public", dotenv=f"HERD_GATEWAY_BIND={BOUND}\n")
    env, _ = _stub_docker(tmp_path, {})
    if base_url is not None:
        env["HERD_BASE_URL"] = base_url
    assert _print_make(root, env, "@echo '$(LOAD_HOST)'") == host + "\n"
    for target in ("test-load", "test-load-ui"):
        _, recipe = _rule(target)
        assert len(recipe) == 1 and "--host $(LOAD_HOST)" in recipe[0], recipe


def test_no_recipe_line_hard_codes_localhost():
    """Every stack-addressing recipe line goes through the derived address."""
    lines = MAKEFILE.read_text().splitlines()
    offenders = [line for line in lines if line.startswith("\t") and "https://localhost" in line]
    assert offenders == []
    _, recipe = _rule("_master-wait-healthy")
    assert any("curl -skf $(GATEWAY_URL)/api/auth/health" in line for line in recipe), recipe


@pytest.mark.skipif(not HAS_MAKE, reason="make is not installed")
def test_bound_address_reaches_every_operator_message_through_real_make(tmp_path):
    """A scratch `.env` with HERD_GATEWAY_BIND set: the dev guard's refusal, the
    gate guard's refusal, the `make logs` note, and the everything banner all
    name https://<that address> and none says localhost."""
    dotenv = f"HERD_GATEWAY_BIND={BOUND}\n"
    url = f"https://{BOUND}"

    root = _checkout(tmp_path / "dev", "herd-public", dotenv=dotenv)
    env, calls = _stub_docker(
        tmp_path / "dev", {"443": [("herd-public-gate", "t")]}, compose_name="herd-public"
    )
    up = _make(root, "up", env)
    assert up.returncode != 0
    assert f"left it serving {url}, built from this checkout" in up.stdout
    assert f"use it as is at {url}, or" in up.stdout
    assert "localhost" not in up.stdout
    assert not _ran_up(calls)

    logs = _make(root, "logs", env)
    assert logs.returncode == 0, logs.stderr
    assert (
        f"compose project herd-public has no containers; {url} is served by "
        "compose project herd-public-gate" in logs.stdout
    )

    _, everything = _rule("everything")
    banner = [line.lstrip("\t") for line in everything[-3:]]
    assert all(line.startswith('@echo "') for line in banner), banner
    printed = _print_make(root, env, *banner)
    assert f"gate stack left running and seeded at {url} ===" in printed
    assert "localhost" not in printed

    wt = _checkout(tmp_path / "gate", "herd-wt", dotenv=dotenv)
    env2, calls2 = _stub_docker(tmp_path / "gate", {"443": [("herd-public-gate", "t", "/w")]})
    gate = _make(wt, "_master-stack-up", env2)
    assert gate.returncode != 0
    assert f"checkout left serving {url}. Either" in gate.stdout
    assert "localhost" not in gate.stdout
    assert not _ran_up(calls2)


# -- the compose Traefik definition -------------------------------------------

EXPECTED_STATIC_FLAGS = [
    "--api.dashboard=true",
    "--api.insecure=true",
    "--entryPoints.web.address=:80",
    "--entryPoints.web.http.redirections.entryPoint.to=websecure",
    "--entryPoints.web.http.redirections.entryPoint.scheme=https",
    "--entryPoints.websecure.address=:443",
    "--providers.docker.endpoint=unix:///var/run/docker.sock",
    "--providers.docker.exposedByDefault=false",
    "--providers.file.filename=/etc/traefik/dynamic.yml",
    "--providers.file.watch=true",
    "--log.level=INFO",
    "--accessLog=true",
]
INTERPOLATED_FLAGS = ("--providers.docker.constraints=", "--providers.docker.network=")


def _traefik(path: Path) -> dict:
    return yaml.safe_load(path.read_text())["services"].get("traefik") or {}


def _flag(command: list[str], name: str) -> str:
    matches = [c for c in command if c.startswith(f"--{name}=")]
    assert len(matches) == 1, (name, command)
    return matches[0].split("=", 1)[1]


def test_traefik_static_flags_are_exactly_the_old_files_settings():
    """Every setting of the deleted traefik.yml, one for one (dashboard on 8080,
    the http to https redirect, TLS through the file provider, access log)."""
    command = _traefik(BASE_COMPOSE)["command"]
    static = [c for c in command if not c.startswith(INTERPOLATED_FLAGS)]
    assert static == EXPECTED_STATIC_FLAGS
    assert len(command) == len(EXPECTED_STATIC_FLAGS) + len(INTERPOLATED_FLAGS)


def test_traefik_discovery_is_constrained_to_its_own_compose_project():
    command = _traefik(BASE_COMPOSE)["command"]
    constraint = _flag(command, "providers.docker.constraints")
    assert constraint.startswith("Label(`com.docker.compose.project`,`" + PROJECT_INTERPOLATION)
    assert constraint.endswith("}`)"), constraint
    assert "needs Docker Compose 2.27.1 or later" in constraint


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
    # A directory here is the root-owned stand-in Docker creates when a Traefik
    # container from before the removal is restarted; see docs/OPERATIONS.md.
    assert not STALE_STATIC_FILE.is_file()


def test_dev_override_does_not_replace_the_traefik_command():
    """An override's `command` REPLACES the base list, constraint included."""
    assert "command" not in _traefik(DEV_OVERRIDE)


@pytest.mark.skipif(not HAS_COMPOSE, reason="docker compose is not installed")
@pytest.mark.parametrize("project", ["herd-public", "herd-public-gate"])
def test_compose_renders_the_project_into_the_traefik_flags(tmp_path, project):
    env = {k: v for k, v in os.environ.items() if k != "COMPOSE_PROJECT_NAME"}
    env["DOCKER_HOST"] = DEAD_DOCKER_HOST
    result = subprocess.run(
        [
            REAL_DOCKER,
            "compose",
            "-f",
            str(BASE_COMPOSE),
            "-p",
            project,
            "config",
            "--format",
            "json",
        ],
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
