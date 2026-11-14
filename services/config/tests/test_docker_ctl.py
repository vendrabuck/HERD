"""Save and Restart's client side (issue #1326, ADR 0017): app.docker_ctl.restart_services.

The config service no longer talks to Docker. These tests run restart_services, and
POST /apply through the app, against a fake restarter: a standard-library HTTP server
on a loopback port that records each request and answers what the test sets. They pin
the one call the service makes (a bare POST /restart), the mapping of every answer to
`{restarted, errors}` with fixed wording, and that the restarter's body is passed
through only when it has exactly that shape.
"""

from __future__ import annotations

import ast
import json
import logging
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from app import docker_ctl
from app.docker_ctl import (
    IN_PROGRESS_ERROR,
    RESTART_PATH,
    UNEXPECTED_BODY_ERROR,
    restart_services,
)
from app.main import app
from httpx import ASGITransport, AsyncClient

APP_DIR = Path(docker_ctl.__file__).resolve().parent
GOOD = {"restarted": ["auth", "inventory"], "errors": ["Failed to restart acl: DockerAPIError"]}
LEAK = "secret-daemon-text /var/run/docker.sock"


class FakeRestarter:
    def __init__(self):
        self.status = 200
        self.body: bytes = json.dumps(GOOD).encode()
        self.hang = threading.Event()  # set: answer; clear and `hold`: never answer
        self.hold = False
        self.requests: list[dict] = []


@pytest.fixture
def restarter(monkeypatch):
    fake = FakeRestarter()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            fake.requests.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "body": self.rfile.read(length),
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                }
            )
            if fake.hold:
                fake.hang.wait(10)
                return
            self.send_response(fake.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(fake.body)))
            self.end_headers()
            self.wfile.write(fake.body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
    monkeypatch.setattr(docker_ctl, "RESTARTER_HOST", "127.0.0.1")
    monkeypatch.setattr(docker_ctl, "RESTARTER_PORT", server.server_address[1])
    yield fake
    fake.hang.set()
    server.shutdown()
    server.server_close()


def test_the_one_call_is_a_bare_post_restart(restarter):
    assert restart_services() == GOOD
    assert len(restarter.requests) == 1
    request = restarter.requests[0]
    assert (request["method"], request["path"], request["body"]) == ("POST", "/restart", b"")
    assert RESTART_PATH == "/restart"
    assert request["headers"]["content-length"] == "0"
    assert set(request["headers"]) <= {"host", "accept-encoding", "content-length"}


def test_the_answer_is_passed_through_when_it_has_exactly_the_shape(restarter):
    restarter.body = json.dumps({"restarted": [], "errors": []}).encode()
    assert restart_services() == {"restarted": [], "errors": []}


def test_a_409_is_the_in_progress_entry(restarter):
    restarter.status = 409
    restarter.body = json.dumps({"detail": LEAK}).encode()

    assert restart_services() == {"restarted": [], "errors": ["A restart is already in progress"]}
    assert IN_PROGRESS_ERROR == "A restart is already in progress"


@pytest.mark.parametrize("status", [201, 204, 400, 404, 405, 500, 502])
def test_another_status_is_named_and_the_body_never_echoed(restarter, status, caplog):
    restarter.status = status
    restarter.body = json.dumps({"restarted": ["x"], "errors": [LEAK]}).encode()
    caplog.set_level(logging.ERROR, logger="app.docker_ctl")

    result = restart_services()

    assert result == {"restarted": [], "errors": [f"The restart service answered HTTP {status}"]}
    assert LEAK not in json.dumps(result)


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        b'"text"',
        b"{}",
        json.dumps({"restarted": []}).encode(),
        json.dumps({"restarted": [], "errors": [], "detail": LEAK}).encode(),
        json.dumps({"restarted": "auth", "errors": []}).encode(),
        json.dumps({"restarted": [], "errors": [1]}).encode(),
        json.dumps({"restarted": [], "errors": [None]}).encode(),
        json.dumps({"restarted": [], "errors": [""]}).encode(),
        json.dumps({"restarted": [], "errors": ["x" * 201]}).encode(),
        json.dumps({"restarted": [], "errors": ["line\nbreak"]}).encode(),
        json.dumps({"restarted": ["a"] * 65, "errors": []}).encode(),
        json.dumps({"restarted": [{"name": LEAK}], "errors": []}).encode(),
        b"\xff\xfe",
        b"{" + b" " * (70 * 1024) + b"}",
    ],
    ids=[
        "not-json",
        "list",
        "string",
        "empty-object",
        "missing-key",
        "extra-key",
        "not-a-list",
        "int-entry",
        "null-entry",
        "empty-entry",
        "long-entry",
        "unprintable-entry",
        "too-many",
        "object-entry",
        "not-utf8",
        "oversized",
    ],
)
def test_any_other_200_body_is_the_unexpected_body_entry(restarter, body):
    restarter.body = body

    result = restart_services()

    assert result == {"restarted": [], "errors": [UNEXPECTED_BODY_ERROR]}
    assert UNEXPECTED_BODY_ERROR == "The restart service answered an unexpected body"


def test_a_refused_connection_is_cannot_reach_with_the_class_only(monkeypatch, caplog):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
    monkeypatch.setattr(docker_ctl, "RESTARTER_HOST", "127.0.0.1")
    monkeypatch.setattr(docker_ctl, "RESTARTER_PORT", free_port)
    caplog.set_level(logging.ERROR, logger="app.docker_ctl")

    result = restart_services()

    assert result == {
        "restarted": [],
        "errors": ["Cannot reach the restart service: ConnectionRefusedError"],
    }
    assert "Connection refused" in caplog.text


def test_an_unresolvable_restarter_is_cannot_reach(monkeypatch):
    monkeypatch.setattr(docker_ctl, "RESTARTER_HOST", "restarter.invalid")

    result = restart_services()

    assert result == {"restarted": [], "errors": ["Cannot reach the restart service: gaierror"]}


def test_a_silent_restarter_times_out(restarter, monkeypatch):
    restarter.hold = True
    monkeypatch.setattr(docker_ctl, "RESTART_TIMEOUT_SECONDS", 0.3)

    result = restart_services()

    assert result == {"restarted": [], "errors": ["Cannot reach the restart service: TimeoutError"]}


def test_the_restarter_address_is_the_compose_service_on_herd_ctl():
    assert (docker_ctl.RESTARTER_HOST, docker_ctl.RESTARTER_PORT) == ("restarter", 8000)


# -- Through the API --


@pytest.fixture
def async_client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _configured_session(client) -> dict:
    from app.config_store import CONFIG_FILE

    Path(CONFIG_FILE).write_text("{}")
    login = await client.post("/login", json={"password": "test-config-pass"})
    return {"Authorization": f"Bearer {login.json()['token']}"}


async def test_apply_answers_the_restarters_result(async_client, restarter):
    headers = await _configured_session(async_client)

    response = await async_client.post("/apply", headers=headers)

    assert (response.status_code, response.json()) == (200, GOOD)


@pytest.mark.parametrize(
    "target",
    [
        "/apply?service=postgres",
        "/apply?project=other&t=0",
    ],
)
async def test_apply_passes_nothing_from_the_request_on(async_client, restarter, target):
    headers = await _configured_session(async_client)
    headers["X-Service"] = "postgres"

    response = await async_client.post(
        target, headers=headers, json={"services": ["postgres"], "project": "other"}
    )

    assert (response.status_code, response.json()) == (200, GOOD)
    assert [(r["method"], r["path"], r["body"]) for r in restarter.requests] == [
        ("POST", "/restart", b"")
    ]
    assert "x-service" not in restarter.requests[0]["headers"]
    assert "authorization" not in restarter.requests[0]["headers"]


async def test_apply_passes_the_409_on_as_an_error_entry(async_client, restarter):
    restarter.status = 409
    headers = await _configured_session(async_client)

    response = await async_client.post("/apply", headers=headers)

    assert (response.status_code, response.json()) == (
        200,
        {"restarted": [], "errors": ["A restart is already in progress"]},
    )


async def test_apply_unconfigured_makes_no_call(async_client, restarter):
    login = await async_client.post("/login", json={"password": "test-config-pass"})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}

    response = await async_client.post("/apply", headers=headers)

    assert response.status_code == 400
    assert restarter.requests == []


async def test_apply_without_a_session_makes_no_call(async_client, restarter):
    response = await async_client.post("/apply")

    assert response.status_code == 401
    assert restarter.requests == []


# -- The config service holds no Docker client --


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
        elif (
            isinstance(node, ast.Call)
            and getattr(node.func, "attr", getattr(node.func, "id", None))
            in ("import_module", "__import__")
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            roots.add(node.args[0].value.split(".")[0])
    return roots


def test_no_module_under_app_imports_docker():
    """No `import docker`, `from docker ...`, or importlib/__import__ of a literal
    `docker` anywhere under services/config/app. A name built at run time is not seen."""
    offenders = {
        path.name: roots
        for path in sorted(APP_DIR.glob("*.py"))
        if "docker" in (roots := _imported_roots(path))
    }
    assert offenders == {}


def test_the_import_scan_sees_each_spelling(tmp_path):
    for source in (
        "import docker\n",
        "import docker.errors as e\n",
        "from docker import from_env\n",
        "import importlib\nimportlib.import_module('docker')\n",
        "__import__('docker.models')\n",
    ):
        probe = tmp_path / "probe.py"
        probe.write_text(source)
        assert "docker" in _imported_roots(probe), source


def test_docker_ctl_imports_only_the_standard_library():
    roots = _imported_roots(Path(docker_ctl.__file__))
    assert roots, "no imports found; the parse is wrong"
    assert roots <= set(sys.stdlib_module_names) | {"__future__"}, roots


def test_docker_is_not_a_dependency_of_the_config_service():
    import tomllib

    pyproject = tomllib.loads((APP_DIR.parent / "pyproject.toml").read_text())
    names = [
        dep.split(">")[0].split("<")[0].split("=")[0].split("[")[0].strip().lower()
        for dep in pyproject["project"]["dependencies"]
    ]
    assert "docker" not in names, names
