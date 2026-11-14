"""The restarter's Docker side (ADR 0017): what it asks the daemon, and what it restarts.

Every test runs `restarter.run_restart` against the fake daemon in fake_docker.py,
which records each request. The invariant tests enumerate the container space a host
can hold (another project's services, infrastructure of this project, a container with
no labels, one with only some labels, a project name that is a prefix of ours) and
assert the recorded requests and the restarted ids, never the restarter's internals.
"""

from __future__ import annotations

import json
import logging
import re
import socket
import threading
import time

import pytest
import restarter
from fake_docker import PROJECT, container

# A, written out here rather than read from RESTART_SERVICES, so dropping a name from
# the product set fails a test instead of silently dropping that name's case.
EXPECTED_RESTART_SERVICES = (
    "acl",
    "ai-orchestrator",
    "auth",
    "cabling",
    "execution",
    "integration",
    "inventory",
    "notifications",
    "reservations",
    "secrets",
    "user-profile",
)
NOT_RESTARTED_HERE = ("config", "traefik", "postgres", "nats", "frontend", "selenium", "restarter")

LIST_PATH = "/v1.44/containers/json"
LIST_QUERY = {"filters": ['{"label":["com.docker.compose.project=herd"]}']}
RESTART_PATH = re.compile(r"/v1\.44/containers/[0-9a-f]{64}/restart")


def _space() -> tuple[list[dict], list[str]]:
    """Every kind of container a host can hold, and the ids that must be restarted."""
    ours = [container(name) for name in EXPECTED_RESTART_SERVICES]
    others = [container(name) for name in NOT_RESTARTED_HERE]
    foreign = [
        container("auth", project="reportportal"),
        container("inventory", project="herd-gate"),  # our name is its prefix
        container("postgres", project="somedb"),
        container("auth", project=None),  # service label only
        container(None, project=PROJECT),  # project label only
        {"Id": "f" * 64, "Names": ["/bare"], "Labels": {}},
        {"Id": "e" * 64, "Names": ["/nulllabels"], "Labels": None},
        {"Id": "d" * 64, "Names": ["/oddlabels"], "Labels": ["not", "a", "map"]},
        "not an object",
    ]
    return ours + others + foreign, [c["Id"] for c in ours]


def _assert_every_request_is_allowed(daemon) -> None:
    for request in daemon.requests:
        if request.method == "GET":
            assert request.path == LIST_PATH, request
            assert request.query == LIST_QUERY, request
        else:
            assert request.method == "POST", request
            assert RESTART_PATH.fullmatch(request.path), request
            assert request.query == {"t": ["30"]}, request


def test_restart_services_is_exactly_the_expected_literal():
    assert restarter.RESTART_SERVICES == frozenset(EXPECTED_RESTART_SERVICES)


@pytest.mark.parametrize("honor_filters", [True, False], ids=["daemon-filters", "daemon-ignores"])
def test_restarts_exactly_the_allowlist_within_the_project(daemon, honor_filters):
    # The invariant: the requests are a subset of {the list with exactly the project
    # filter, a restart of a 64-hex id with t=30}, and the restarted ids are exactly A
    # intersected with containers(P), even when the daemon ignores the filter.
    daemon.containers, expected_ids = _space()
    daemon.honor_filters = honor_filters

    result = restarter.run_restart(PROJECT, daemon.socket_path)

    assert result == {"restarted": sorted(EXPECTED_RESTART_SERVICES), "errors": []}
    assert daemon.restarted_ids() == expected_ids
    assert len(daemon.list_calls()) == 1
    _assert_every_request_is_allowed(daemon)


def test_only_running_members_of_a_are_restarted(daemon):
    daemon.containers = [container("auth"), container("postgres"), container("selenium")]

    result = restarter.run_restart(PROJECT, daemon.socket_path)

    assert result == {"restarted": ["auth"], "errors": []}
    assert daemon.restarted_ids() == [container("auth")["Id"]]
    _assert_every_request_is_allowed(daemon)


@pytest.mark.parametrize("project", [None, "", "   ", "\t\n"])
def test_unset_or_blank_project_refuses_with_no_docker_request(daemon, project, caplog):
    daemon.containers, _ = _space()
    caplog.set_level(logging.ERROR, logger="restarter")

    result = restarter.run_restart(project, daemon.socket_path)

    assert result == {"restarted": [], "errors": [restarter.PROJECT_UNSET_ERROR]}
    assert restarter.PROJECT_UNSET_ERROR == (
        "HERD_COMPOSE_PROJECT is unset or blank; refusing a restart that is not scoped "
        "to one compose project"
    )
    assert daemon.requests == []
    assert restarter.PROJECT_UNSET_ERROR in caplog.text


@pytest.mark.parametrize(
    "odd_id",
    [
        "../../containers/create",
        "A" * 64,
        "ab" * 6,
        "0" * 63,
        "0" * 65,
        "0" * 63 + "g",
        "0" * 64 + "\n",
        "0" * 64 + "/../create",
        "0" * 32 + "?t=0&" + "0" * 27,
        None,
        12345,
        ["0" * 64],
    ],
)
def test_an_odd_container_id_never_reaches_a_path(daemon, odd_id):
    good = container("inventory")
    daemon.containers = [container("auth", cid=odd_id), good]

    result = restarter.run_restart(PROJECT, daemon.socket_path)

    assert result == {
        "restarted": ["inventory"],
        "errors": ["Failed to restart auth: UnexpectedContainerId"],
    }
    assert daemon.restarted_ids() == [good["Id"]]
    _assert_every_request_is_allowed(daemon)


def test_missing_socket_is_cannot_connect_with_the_class_only(tmp_path, caplog):
    missing = str(tmp_path / "no-such.sock")
    caplog.set_level(logging.ERROR, logger="restarter")

    result = restarter.run_restart(PROJECT, missing)

    assert result == {"restarted": [], "errors": ["Cannot connect to Docker: FileNotFoundError"]}
    assert "No such file" in caplog.text


def test_list_failure_is_cannot_list_with_the_class_only(daemon, caplog):
    daemon.containers = [container("auth")]
    daemon.list_status = 500
    caplog.set_level(logging.ERROR, logger="restarter")

    result = restarter.run_restart(PROJECT, daemon.socket_path)

    assert result == {"restarted": [], "errors": ["Cannot list containers: DockerAPIError"]}
    assert daemon.restarted_ids() == []
    assert "HTTP 500" in caplog.text


@pytest.mark.parametrize(
    ("raw", "error_class"),
    [
        (b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\nnope", "JSONDecodeError"),
        (b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}", "UnexpectedDockerResponse"),
        (b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n[]", "UnexpectedDockerResponse"),
        (b"HTTP/1.1 200 OK\r\nContent-Length: x\r\n\r\n[]", "UnexpectedDockerResponse"),
        (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: gzip\r\n\r\n[]", "UnexpectedDockerResponse"),
        (
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\nzz\r\n",
            "UnexpectedDockerResponse",
        ),
        (b"SSH-2.0-OpenSSH\r\n\r\n", "UnexpectedDockerResponse"),
        (b"HTTP/1.1 200 OK\r\nno colon here\r\n\r\n[]", "UnexpectedDockerResponse"),
        (b"HTTP/1.1 200 OK\r\nContent-Le", "UnexpectedDockerResponse"),
        (b"", "UnexpectedDockerResponse"),
    ],
    ids=[
        "not-json",
        "an-object",
        "truncated-body",
        "bad-length",
        "other-encoding",
        "bad-chunk",
        "not-http",
        "bad-header",
        "truncated-head",
        "closed-at-once",
    ],
)
def test_a_malformed_list_answer_is_cannot_list(daemon, raw, error_class):
    daemon.list_raw = raw

    result = restarter.run_restart(PROJECT, daemon.socket_path)

    assert result == {"restarted": [], "errors": [f"Cannot list containers: {error_class}"]}
    assert daemon.restarted_ids() == []


def test_a_chunked_list_answer_is_read(daemon):
    daemon.containers = [container("auth"), container("secrets")]
    daemon.list_chunked = True

    result = restarter.run_restart(PROJECT, daemon.socket_path)

    assert result == {"restarted": ["auth", "secrets"], "errors": []}


def test_an_oversized_list_answer_is_refused(daemon, monkeypatch):
    monkeypatch.setattr(restarter, "MAX_DOCKER_RESPONSE_BYTES", 512)
    daemon.containers = [container("auth")] * 20

    result = restarter.run_restart(PROJECT, daemon.socket_path)

    assert result == {
        "restarted": [],
        "errors": ["Cannot list containers: UnexpectedDockerResponse"],
    }
    assert daemon.restarted_ids() == []


def test_a_hung_daemon_times_out_within_the_deadline(daemon, monkeypatch):
    monkeypatch.setattr(restarter, "LIST_DEADLINE_SECONDS", 0.5)
    daemon.list_gate = threading.Event()  # never set during the call
    daemon.containers = [container("auth")]

    started = time.monotonic()
    result = restarter.run_restart(PROJECT, daemon.socket_path)

    assert result == {"restarted": [], "errors": ["Cannot list containers: TimeoutError"]}
    assert time.monotonic() - started < 5


def test_a_trickling_daemon_is_cut_off_at_the_whole_answer_deadline(tmp_path, monkeypatch):
    # A per-read timeout alone would never fire here: a byte arrives every 0.1 s. The
    # deadline covers the whole answer, so the single-flight lock cannot be held past
    # WORST_CASE_SECONDS by a slow daemon.
    monkeypatch.setattr(restarter, "LIST_DEADLINE_SECONDS", 0.6)
    path = str(tmp_path / "trickle.sock")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    listener.listen(1)
    stop = threading.Event()

    def trickle():
        conn, _ = listener.accept()
        with conn:
            conn.recv(65536)
            for byte in b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n" + b" " * 1000:
                if stop.is_set():
                    return
                try:
                    conn.sendall(bytes([byte]))
                except OSError:
                    return
                time.sleep(0.1)

    thread = threading.Thread(target=trickle, daemon=True)
    thread.start()
    try:
        started = time.monotonic()
        result = restarter.run_restart(PROJECT, path)
        elapsed = time.monotonic() - started
    finally:
        stop.set()
        listener.close()

    assert result == {"restarted": [], "errors": ["Cannot list containers: TimeoutError"]}
    assert elapsed < 3


def test_a_failed_restart_names_the_service_and_class_and_the_rest_go_on(daemon, caplog):
    daemon.containers = [container("auth"), container("inventory"), container("secrets")]
    daemon.restart_status[container("inventory")["Id"]] = 500
    caplog.set_level(logging.ERROR, logger="restarter")

    result = restarter.run_restart(PROJECT, daemon.socket_path)

    assert result == {
        "restarted": ["auth", "secrets"],
        "errors": ["Failed to restart inventory: DockerAPIError"],
    }
    # The daemon's text goes to the log, never to the answer.
    assert "daemon secret text" not in json.dumps(result)
    assert "daemon secret text" in caplog.text


def test_restart_is_accepted_on_any_2xx_and_refused_otherwise(daemon):
    daemon.containers = [container("auth"), container("acl"), container("cabling")]
    daemon.restart_status[container("acl")["Id"]] = 200
    daemon.restart_status[container("cabling")["Id"]] = 304

    result = restarter.run_restart(PROJECT, daemon.socket_path)

    assert result == {
        "restarted": ["acl", "auth"],
        "errors": ["Failed to restart cabling: DockerAPIError"],
    }


def test_worst_case_covers_the_list_and_every_restart():
    assert restarter.WORST_CASE_SECONDS == (
        restarter.LIST_DEADLINE_SECONDS
        + len(restarter.RESTART_SERVICES) * restarter.RESTART_DEADLINE_SECONDS
    )
    assert restarter.RESTART_DEADLINE_SECONDS > restarter.STOP_TIMEOUT_SECONDS


def test_restarter_imports_only_the_standard_library():
    import ast
    import sys
    from pathlib import Path

    tree = ast.parse(Path(restarter.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    assert imported, "no imports found; the parse is wrong"
    assert imported <= set(sys.stdlib_module_names) | {"__future__"}, imported
