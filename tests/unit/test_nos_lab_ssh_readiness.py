"""SSH readiness of the NOS lab (issue #1263). No lab, no Docker.

The advisory nos-dialect job failed on its first SR Linux test with an SSH
ConnectionResetError during the banner exchange: `make nos-up` is
`docker compose up --wait`, and the SR Linux healthcheck proved the baseline
landed without ever opening an SSH connection. Two fixes, pinned here:

- At the source, both lab nodes' healthchecks carry an SSH probe: the FRR
  node's `nc -z` (issue #783) and, now, the SR Linux node's banner read,
  infra/nos-test/srl/ssh_ready.sh. The static pins stop an edit from dropping
  either silently; the probe tests run the real script against local fake
  servers, so a banner read that degrades into a bare port check fails here.
- In the tests, every test-side netmiko session opens through
  tests/nos_lab/_lab_ssh.py's `open_lab_session`, which retries a transport
  failure three times, two seconds apart, and never retries an
  authentication failure.

Limits: the probe tests run the script in the host's own network namespace,
so its srbase-mgmt branch (entered only inside the SR Linux container) is not
exercised here; the cold-boot runs of `make nos-test-dialect` are what prove
it against the real image.
"""

from __future__ import annotations

import ast
import socket
import struct
import subprocess
import threading
from pathlib import Path

import pytest
import yaml
from netmiko import NetmikoAuthenticationException, NetmikoTimeoutException

from tests.nos_lab import _lab_ssh

REPO_ROOT = Path(__file__).resolve().parents[2]
NOS_COMPOSE_PATH = REPO_ROOT / "infra" / "nos-test" / "docker-compose.yml"
SSH_READY_PATH = REPO_ROOT / "infra" / "nos-test" / "srl" / "ssh_ready.sh"
SSH_READY_IN_CONTAINER = "/etc/opt/srlinux/ssh_ready.sh"
NOS_LAB_TESTS_DIR = REPO_ROOT / "tests" / "nos_lab"


def _healthcheck_command(service: str) -> str:
    data = yaml.safe_load(NOS_COMPOSE_PATH.read_text())
    test = data["services"][service]["healthcheck"]["test"]
    assert isinstance(test, list) and test[0] == "CMD-SHELL", (service, test)
    assert len(test) == 2, (service, test)
    return test[1]


# ---------------------------------------------------------------------------
# Static pins on the lab compose file.
# ---------------------------------------------------------------------------


def test_srl_healthcheck_keeps_the_baseline_check_and_adds_the_ssh_banner_probe():
    command = _healthcheck_command("srl")
    parts = [part.strip() for part in command.split("&&")]
    assert parts == [
        "test -f /etc/opt/srlinux/config.json",
        "sr_cli 'info /interface ethernet-1/2 vlan-tagging' | grep -q true",
        f"bash {SSH_READY_IN_CONTAINER} 22",
    ], (
        "the SR Linux healthcheck must prove the baseline landed AND that sshd "
        "sends a banner on port 22 (issue #1263); found: " + command
    )


def test_srl_mounts_the_ssh_probe_where_the_healthcheck_runs_it():
    data = yaml.safe_load(NOS_COMPOSE_PATH.read_text())
    volumes = data["services"]["srl"]["volumes"]
    assert f"./srl/ssh_ready.sh:{SSH_READY_IN_CONTAINER}:ro" in volumes, volumes


def test_frr_healthcheck_keeps_its_ssh_port_probe():
    command = _healthcheck_command("frr")
    parts = [part.strip() for part in command.split("&&")]
    assert "nc -z localhost 22" in parts, (
        "the FRR healthcheck must keep its sshd probe (issue #783); found: " + command
    )


def test_ssh_probe_reads_a_banner_rather_than_checking_the_port():
    text = SSH_READY_PATH.read_text()
    assert text.startswith("#!/bin/bash\n")
    assert '"/dev/tcp/$1/$2"' in text
    assert "SSH-*) exit 0" in text
    assert "srbase-mgmt" in text
    result = subprocess.run(["bash", "-n", str(SSH_READY_PATH)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# The probe, run for real against fake servers on 127.0.0.1.
# ---------------------------------------------------------------------------


class _FakeServer:
    """A one-port TCP server on 127.0.0.1 that answers every connection with
    `behavior(conn)` until stopped."""

    def __init__(self, behavior):
        self._behavior = behavior
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            try:
                self._behavior(conn)
            finally:
                conn.close()

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=5)
        self._sock.close()


def _send_banner(conn):
    conn.sendall(b"SSH-2.0-OpenSSH_9.6\r\n")


def _send_http(conn):
    conn.sendall(b"HTTP/1.1 400 Bad Request\r\n")


def _close_at_once(conn):
    pass


def _reset(conn):
    # SO_LINGER with a zero timeout makes close() send RST, which is what the
    # failing SR Linux node did to paramiko during the banner exchange.
    conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))


def _stay_silent(conn):
    conn.recv(64)


def _run_probe(port: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SSH_READY_PATH), str(port)],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_probe_passes_when_the_server_sends_an_ssh_banner():
    with _FakeServer(_send_banner) as server:
        result = _run_probe(server.port)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "behavior",
    [_reset, _close_at_once, _send_http, _stay_silent],
    ids=["reset-on-connect", "close-without-banner", "non-ssh-line", "silent"],
)
def test_probe_fails_when_the_port_listens_but_sends_no_ssh_banner(behavior):
    with _FakeServer(behavior) as server:
        result = _run_probe(server.port)
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert result.stderr.strip().endswith(f"no SSH banner on port {server.port}")


def test_probe_fails_when_nothing_listens():
    with _FakeServer(_send_banner) as server:
        port = server.port
    result = _run_probe(port)
    assert result.returncode == 1
    assert result.stderr.strip() == f"ssh_ready.sh: no SSH banner on port {port}"


# ---------------------------------------------------------------------------
# The retry helper every test-side netmiko session goes through.
# ---------------------------------------------------------------------------


class _ScriptedConnect:
    """Stands in for netmiko's ConnectHandler: raises each scripted exception
    in turn, then returns a session object."""

    def __init__(self, *outcomes):
        self._outcomes = list(outcomes)
        self.calls: list[dict] = []
        self.session = object()

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self._outcomes:
            raise self._outcomes.pop(0)
        return self.session


@pytest.fixture
def sleeps(monkeypatch):
    recorded: list[float] = []
    monkeypatch.setattr(_lab_ssh.time, "sleep", recorded.append)
    return recorded


_KWARGS = {
    "device_type": "nokia_srl",
    "host": "127.0.0.1",
    "port": 2223,
    "username": "admin",
    "password": "password",
    "fast_cli": False,
}


def test_a_boot_time_reset_twice_then_a_session_is_returned(monkeypatch, sleeps):
    connect = _ScriptedConnect(
        ConnectionResetError(104, "Connection reset by peer"),
        EOFError(),
    )
    monkeypatch.setattr(_lab_ssh, "ConnectHandler", connect)

    assert _lab_ssh.open_lab_session("the SR Linux node", **_KWARGS) is connect.session
    assert connect.calls == [_KWARGS, _KWARGS, _KWARGS]
    assert sleeps == [2.0, 2.0]


def test_three_transport_failures_fail_with_the_attempt_count_and_last_class(monkeypatch, sleeps):
    last = NetmikoTimeoutException("TCP connection to device failed.")
    connect = _ScriptedConnect(ConnectionResetError(104, "reset"), OSError("refused"), last)
    monkeypatch.setattr(_lab_ssh, "ConnectHandler", connect)

    with pytest.raises(_lab_ssh.LabConnectError) as raised:
        _lab_ssh.open_lab_session("the SR Linux node", **_KWARGS)

    assert str(raised.value) == (
        "could not open an SSH session to the SR Linux node after 3 attempts "
        "2 s apart; last error: NetmikoTimeoutException"
    )
    assert raised.value.__cause__ is last
    assert len(connect.calls) == 3
    assert sleeps == [2.0, 2.0]


def test_an_authentication_failure_is_never_retried(monkeypatch, sleeps):
    refused = NetmikoAuthenticationException("Authentication to device failed.")
    connect = _ScriptedConnect(refused)
    monkeypatch.setattr(_lab_ssh, "ConnectHandler", connect)

    with pytest.raises(NetmikoAuthenticationException) as raised:
        _lab_ssh.open_lab_session("the FRR node", **_KWARGS)

    assert raised.value is refused
    assert len(connect.calls) == 1
    assert sleeps == []


def test_an_unrelated_error_is_not_retried(monkeypatch, sleeps):
    connect = _ScriptedConnect(ValueError("Unsupported 'device_type'"))
    monkeypatch.setattr(_lab_ssh, "ConnectHandler", connect)

    with pytest.raises(ValueError):
        _lab_ssh.open_lab_session("the FRR node", **_KWARGS)

    assert len(connect.calls) == 1
    assert sleeps == []


def _connect_handler_calls(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(), filename=str(path))
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name == "ConnectHandler":
            lines.append(node.lineno)
    return lines


def test_no_nos_lab_test_opens_a_netmiko_session_outside_the_helper():
    offenders = {
        path.name: lines
        for path in sorted(NOS_LAB_TESTS_DIR.glob("*.py"))
        if path.name != "_lab_ssh.py" and (lines := _connect_handler_calls(path))
    }
    assert not offenders, (
        "test-side netmiko sessions under tests/nos_lab must open through "
        "_lab_ssh.open_lab_session (issue #1263); bare ConnectHandler calls at "
        f"{offenders}"
    )
    assert _connect_handler_calls(NOS_LAB_TESTS_DIR / "_lab_ssh.py"), (
        "the helper itself no longer calls ConnectHandler"
    )
