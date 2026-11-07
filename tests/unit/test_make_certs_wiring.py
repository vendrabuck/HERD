"""Pins for the gateway's generated TLS certificate (issue #1239).

The repository never tracked `infra/traefik/certs/server.key`, so every fresh
clone's Traefik failed to load the tracked chain and served its own
`TRAEFIK DEFAULT CERT`; and `dynamic.yml` set no default certificate, so a
client without SNI (every client by IP address) got the built-in one even
where the key existed. The fix:

- `make certs` (`scripts/make_certs.sh`) writes `server.key` and a ten-year
  self-signed `server-chain.crt` when the key is ABSENT, naming localhost,
  127.0.0.1, the HERD_GATEWAY_BIND address, and CERT_SANS; with both files
  present it runs no openssl and changes nothing; it never rewrites an
  existing key unless CERT_FORCE=1;
- `make up`, `dev`, `prod`, and the gate's `_master-stack-up` depend on it;
- `dynamic.yml` names the same pair for SNI clients and as the default
  store's `defaultCertificate`;
- no certificate or key is tracked, only the directory's README.

Everything runs through real `make` in scratch copies of the checkout (the
Makefile and the two scripts it calls), with a stub `docker` on PATH and
DOCKER_HOST on a dead socket, so no stack and no daemon is touched. Files are
read back with the real `openssl`.
"""

import hashlib
import os
import shutil
import stat
import subprocess
import textwrap
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = REPO_ROOT / "Makefile"
CERTS_SCRIPT = REPO_ROOT / "scripts" / "make_certs.sh"
PORTS_SCRIPT = REPO_ROOT / "scripts" / "check_gateway_ports_free.sh"
DYNAMIC = REPO_ROOT / "infra" / "traefik" / "dynamic.yml"
BASE_COMPOSE = REPO_ROOT / "docker-compose.yml"

HAS_MAKE = shutil.which("make") is not None
HAS_OPENSSL = shutil.which("openssl") is not None
HAS_GIT = shutil.which("git") is not None
DEAD_DOCKER_HOST = "unix:///nonexistent/herd-test-no-daemon.sock"
BOUND = "192.0.2.10"  # TEST-NET-1, never a real host
CERTS = Path("infra") / "traefik" / "certs"
KEY_NAME = "server.key"
CRT_NAME = "server-chain.crt"
CONTAINER_DIR = "/etc/traefik/certs"
BASE_SANS = {"DNS:localhost", "IP Address:127.0.0.1"}
TEN_YEARS = timedelta(days=3650)

needs_make = pytest.mark.skipif(not HAS_MAKE, reason="make is not installed")
needs_openssl = pytest.mark.skipif(not HAS_OPENSSL, reason="openssl is not installed")


def _checkout(tmp_path: Path, dotenv: str | None = None) -> Path:
    """A scratch copy of the checkout's Makefile and the scripts its start
    targets run, with a scratch `.env` holding `dotenv` when given."""
    root = tmp_path / "herd-public"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(MAKEFILE, root / "Makefile")
    shutil.copy2(CERTS_SCRIPT, root / "scripts" / CERTS_SCRIPT.name)
    shutil.copy2(PORTS_SCRIPT, root / "scripts" / PORTS_SCRIPT.name)
    if dotenv is not None:
        (root / ".env").write_text(dotenv)
    return root


def _env(tmp_path: Path, *, fake_openssl: bool = False) -> tuple[dict, Path]:
    """The environment for a scratch `make`: a stub `docker` that records and
    succeeds, and, with `fake_openssl`, an `openssl` that only records."""
    bin_dir = tmp_path / "stub-bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    names = ["docker"] + (["openssl"] if fake_openssl else [])
    for name in names:
        stub = bin_dir / name
        # `docker compose config` names the project, so the ports guard ahead of
        # `certs` in `make dev` resolves it; `docker ps` finds no holder.
        answer = 'case " $* " in *" config "*) echo "name: herd-public" ;; esac\n'
        stub.write_text(
            textwrap.dedent(
                f"""\
                #!/bin/sh
                echo "{name} $*" >> "{log}"
                """
            )
            + (answer if name == "docker" else "")
            + "exit 0\n"
        )
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    for name in (
        "COMPOSE_PROJECT_NAME",
        "HERD_GATE_PROJECT",
        "MAKEFLAGS",
        "MFLAGS",
        "HERD_GATEWAY_BIND",
        "HERD_CERT_BIND",
        "CERT_SANS",
        "CERT_FORCE",
    ):
        env.pop(name, None)
    env["DOCKER_HOST"] = DEAD_DOCKER_HOST
    env["HERD_BUILD"] = "test"
    env["HERD_BUILD_DATE"] = ""
    return env, log


def _make(root: Path, env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["make", "-s", "-C", str(root), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _calls(log: Path, tool: str) -> list[str]:
    if not log.exists():
        return []
    return [line for line in log.read_text().splitlines() if line.startswith(f"{tool} ")]


def _digests(root: Path) -> dict[str, str]:
    certs = root / CERTS
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(certs.iterdir())
        if p.is_file()
    }


def _openssl(*args: str) -> str:
    result = subprocess.run(
        ["openssl", *args], capture_output=True, text=True, timeout=30, check=True
    )
    return result.stdout


def _sans(crt: Path) -> set[str]:
    text = _openssl("x509", "-in", str(crt), "-noout", "-ext", "subjectAltName")
    body = text.split("\n", 1)[1]
    return {entry.strip() for entry in body.split(",") if entry.strip()}


def _validity(crt: Path) -> timedelta:
    text = _openssl("x509", "-in", str(crt), "-noout", "-startdate", "-enddate")
    fields = dict(line.split("=", 1) for line in text.splitlines())
    fmt = "%b %d %H:%M:%S %Y %Z"
    start = datetime.strptime(" ".join(fields["notBefore"].split()), fmt)
    end = datetime.strptime(" ".join(fields["notAfter"].split()), fmt)
    return end - start


def _key_matches(crt: Path, key: Path) -> bool:
    from_crt = _openssl("x509", "-in", str(crt), "-noout", "-pubkey")
    from_key = _openssl("pkey", "-in", str(key), "-pubout")
    return from_crt == from_key


def _rule(target: str) -> list[str]:
    """The prerequisites of `target`'s rule, its help comment dropped."""
    for line in MAKEFILE.read_text().splitlines():
        if line.startswith(f"{target}:") and not line.startswith(f"{target}::"):
            return line[len(target) + 1 :].split("##")[0].split()
    raise AssertionError(f"no rule for {target}")


# -- the wiring ---------------------------------------------------------------


@pytest.mark.parametrize("target", ["dev", "prod", "_master-stack-up"])
def test_stack_start_targets_depend_on_certs_after_their_ports_guard(target):
    prereqs = _rule(target)
    assert "certs" in prereqs, prereqs
    guard = (
        "_assert-gate-ports-free"
        if target == "_master-stack-up"
        else ("_assert-gateway-ports-free")
    )
    assert prereqs.index(guard) < prereqs.index("certs"), prereqs


def test_certs_is_phony_and_calls_only_the_script():
    makefile = MAKEFILE.read_text()
    phony = makefile.split(".PHONY:", 1)[1].split("\n\n", 1)[0]
    assert "certs" in phony.replace("\\", " ").split()
    lines = makefile.splitlines()
    start = lines.index(next(line for line in lines if line.startswith("certs:")))
    recipe = []
    for body in lines[start + 1 :]:
        if not body.startswith("\t"):
            break
        recipe.append(body)
    assert recipe == [
        '\t@HERD_CERT_BIND="$(GATEWAY_BIND)" CERT_SANS="$(CERT_SANS)" '
        'CERT_FORCE="$(CERT_FORCE)" sh scripts/make_certs.sh infra/traefik/certs'
    ]


@needs_make
@pytest.mark.parametrize("target", ["up", "dev", "prod"])
def test_start_targets_reach_make_certs_through_real_make(tmp_path, target):
    """`make -n <target>` on a key-less checkout lists the certs recipe ahead of
    `docker compose up`, so a fresh clone is served the generated certificate."""
    root = _checkout(tmp_path)
    env, log = _env(tmp_path)
    result = _make(root, env, "-n", target)
    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout
    assert "sh scripts/make_certs.sh infra/traefik/certs" in out, out
    assert out.index("make_certs.sh") < out.index("up --build -d"), out
    assert not (root / CERTS).exists()


@needs_make
def test_dry_run_of_certs_writes_nothing(tmp_path):
    root = _checkout(tmp_path)
    env, log = _env(tmp_path)
    result = _make(root, env, "-n", "certs")
    assert result.returncode == 0, result.stderr
    assert "make_certs.sh" in result.stdout
    assert not (root / CERTS).exists()


@needs_make
def test_certs_is_listed_by_the_bare_make_help(tmp_path):
    root = _checkout(tmp_path)
    env, _ = _env(tmp_path)
    result = _make(root, env, "help")
    assert result.returncode == 0, result.stderr
    assert any(line.split()[:1] == ["certs"] for line in result.stdout.splitlines())


# -- an existing key ----------------------------------------------------------


@needs_make
@pytest.mark.parametrize("target", ["certs", "dev"])
def test_present_key_and_certificate_run_no_openssl_and_change_nothing(tmp_path, target):
    """The host case: both files exist (a dummy pair here), so `make certs`, and
    `make dev` through it, print one line, call no openssl, and leave both
    files byte for byte, even with a bind address that is not in them."""
    root = _checkout(tmp_path, dotenv=f"HERD_GATEWAY_BIND={BOUND}\n")
    certs = root / CERTS
    certs.mkdir(parents=True)
    (certs / KEY_NAME).write_text("dummy key, not PEM\n")
    (certs / CRT_NAME).write_text("dummy certificate, not PEM\n")
    before = _digests(root)
    env, log = _env(tmp_path, fake_openssl=True)
    env["CERT_SANS"] = "DNS:lab.example.com"
    result = _make(root, env, target)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _calls(log, "openssl") == []
    assert _digests(root) == before
    lines = [line for line in result.stdout.splitlines() if "TLS certificate" in line]
    assert lines == [
        "TLS certificate present: infra/traefik/certs/server.key and "
        "infra/traefik/certs/server-chain.crt exist, nothing generated "
        "(CERT_FORCE=1 regenerates both)."
    ]
    if target == "certs":
        assert result.stdout == lines[0] + "\n"
    else:
        assert "docker compose up --build -d" in _calls(log, "docker")


@needs_make
@needs_openssl
def test_missing_certificate_is_issued_for_the_existing_key(tmp_path):
    """A checkout that supplied its own key for the certificates the repository
    used to track loses those certificates on pull: the key is kept and a
    certificate is issued for it."""
    root = _checkout(tmp_path, dotenv=f"HERD_GATEWAY_BIND={BOUND}\n")
    certs = root / CERTS
    certs.mkdir(parents=True)
    _openssl(
        "genpkey",
        "-algorithm",
        "RSA",
        "-pkeyopt",
        "rsa_keygen_bits:2048",
        "-out",
        str(certs / KEY_NAME),
    )
    key_before = _digests(root)[KEY_NAME]
    env, _ = _env(tmp_path)
    result = _make(root, env, "certs")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "the key is unchanged" in result.stdout
    assert _digests(root)[KEY_NAME] == key_before
    assert _key_matches(certs / CRT_NAME, certs / KEY_NAME)
    assert _sans(certs / CRT_NAME) == BASE_SANS | {f"IP Address:{BOUND}"}


# -- a fresh clone ------------------------------------------------------------


@needs_make
@needs_openssl
@pytest.mark.parametrize("source", ["dotenv", "environment", "command line"])
def test_fresh_checkout_gets_a_ten_year_certificate_naming_the_bind_address(tmp_path, source):
    dotenv = f"HERD_GATEWAY_BIND={BOUND}\n" if source == "dotenv" else None
    root = _checkout(tmp_path, dotenv=dotenv)
    env, _ = _env(tmp_path)
    args = ["certs"]
    if source == "environment":
        env["HERD_GATEWAY_BIND"] = BOUND
    elif source == "command line":
        args.append(f"HERD_GATEWAY_BIND={BOUND}")
    result = _make(root, env, *args)
    assert result.returncode == 0, result.stdout + result.stderr
    certs = root / CERTS
    crt, key = certs / CRT_NAME, certs / KEY_NAME
    assert sorted(p.name for p in certs.iterdir()) == [CRT_NAME, KEY_NAME]
    assert _sans(crt) == BASE_SANS | {f"IP Address:{BOUND}"}
    assert abs(_validity(crt) - TEN_YEARS) <= timedelta(days=1)
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert _key_matches(crt, key)
    subject = _openssl("x509", "-in", str(crt), "-noout", "-subject", "-issuer")
    assert subject.count("CN = HERD dev gateway") == 2, subject  # self-signed
    text = _openssl("x509", "-in", str(crt), "-noout", "-text")
    assert "CA:FALSE" in text and "TLS Web Server Authentication" in text
    assert "sudo update-ca-certificates" in result.stdout
    assert "Trusted Root Certification Authorities" in result.stdout

    first = _digests(root)
    again = _make(root, env, *args)
    assert again.returncode == 0, again.stderr
    assert _digests(root) == first
    assert again.stdout.startswith("TLS certificate present:")
    assert len(again.stdout.splitlines()) == 1


@needs_make
@needs_openssl
@pytest.mark.parametrize("bind", [None, "", "0.0.0.0"])
def test_every_interface_bind_names_only_localhost(tmp_path, bind):
    dotenv = None if bind is None else f"HERD_GATEWAY_BIND={bind}\n"
    root = _checkout(tmp_path, dotenv=dotenv)
    env, _ = _env(tmp_path)
    result = _make(root, env, "certs")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _sans(root / CERTS / CRT_NAME) == BASE_SANS


@needs_make
@needs_openssl
def test_cert_sans_entries_are_added_once_each(tmp_path):
    root = _checkout(tmp_path, dotenv=f"HERD_GATEWAY_BIND={BOUND}\n")
    env, _ = _env(tmp_path)
    sans = f"DNS:lab.example.com, IP:198.51.100.20,DNS:localhost,IP:{BOUND},"
    result = _make(root, env, "certs", f"CERT_SANS={sans}")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _sans(root / CERTS / CRT_NAME) == BASE_SANS | {
        f"IP Address:{BOUND}",
        "DNS:lab.example.com",
        "IP Address:198.51.100.20",
    }
    text = _openssl("x509", "-in", str(root / CERTS / CRT_NAME), "-noout", "-ext", "subjectAltName")
    assert text.count("localhost") == 1 and text.count(BOUND) == 1


@needs_make
@pytest.mark.parametrize("entry", ["lab.example.com", "URI:https://x", "DNS:", "IP:"])
def test_malformed_cert_sans_entry_is_refused_and_writes_nothing(tmp_path, entry):
    root = _checkout(tmp_path)
    env, log = _env(tmp_path, fake_openssl=True)
    result = _make(root, env, "certs", f"CERT_SANS=DNS:ok.example.com,{entry}")
    assert result.returncode != 0
    assert f"CERT_SANS entry '{entry}' is not DNS:<name> or IP:<address>" in result.stderr
    assert _calls(log, "openssl") == []
    assert not (root / CERTS).exists()


@needs_make
@needs_openssl
def test_cert_force_regenerates_both_and_says_so(tmp_path):
    root = _checkout(tmp_path)
    env, _ = _env(tmp_path)
    assert _make(root, env, "certs").returncode == 0
    first = _digests(root)
    result = _make(root, env, "certs", "CERT_FORCE=1", f"HERD_GATEWAY_BIND={BOUND}")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "CERT_FORCE: regenerating the gateway key and certificate over the existing" in (
        result.stdout
    )
    second = _digests(root)
    assert second[KEY_NAME] != first[KEY_NAME] and second[CRT_NAME] != first[CRT_NAME]
    certs = root / CERTS
    assert _key_matches(certs / CRT_NAME, certs / KEY_NAME)
    assert _sans(certs / CRT_NAME) == BASE_SANS | {f"IP Address:{BOUND}"}


@needs_make
@pytest.mark.parametrize("value", ["0", "false", "no"])
def test_cert_force_off_values_do_not_regenerate(tmp_path, value):
    root = _checkout(tmp_path)
    certs = root / CERTS
    certs.mkdir(parents=True)
    (certs / KEY_NAME).write_text("k\n")
    (certs / CRT_NAME).write_text("c\n")
    env, log = _env(tmp_path, fake_openssl=True)
    result = _make(root, env, "certs", f"CERT_FORCE={value}")
    assert result.returncode == 0, result.stderr
    assert _calls(log, "openssl") == []
    assert result.stdout.startswith("TLS certificate present:")


# -- Traefik's configuration --------------------------------------------------


def test_dynamic_config_serves_the_pair_to_sni_and_no_sni_clients():
    tls = yaml.safe_load(DYNAMIC.read_text())["tls"]
    pair = {"certFile": f"{CONTAINER_DIR}/{CRT_NAME}", "keyFile": f"{CONTAINER_DIR}/{KEY_NAME}"}
    assert tls["certificates"] == [pair]
    assert tls["stores"]["default"]["defaultCertificate"] == pair


def test_compose_mounts_the_directory_make_certs_writes():
    traefik = yaml.safe_load(BASE_COMPOSE.read_text())["services"]["traefik"]
    assert f"./{CERTS.as_posix()}:{CONTAINER_DIR}:ro" in traefik["volumes"]
    assert "--providers.file.filename=/etc/traefik/dynamic.yml" in traefik["command"]


@pytest.mark.skipif(not HAS_GIT, reason="git is not installed")
def test_no_certificate_or_key_is_tracked():
    """Generated files would otherwise overwrite tracked ones and dirty every
    clone; the README keeps the directory, so Docker never creates it as root."""
    tracked = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", CERTS.as_posix()],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout.split()
    assert tracked == [f"{CERTS.as_posix()}/README.md"]
