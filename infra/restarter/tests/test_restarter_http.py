"""The restarter's HTTP surface (ADR 0017), driven through the real server.

Each test talks to `restarter.RestarterServer` on a loopback port over a raw socket,
so it can send what a well-behaved client never would (an unknown method, a chunked
body, a duplicated Content-Length), and the server reaches the fake daemon of
fake_docker.py, whose request record is the evidence of what reached Docker.
"""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest
import restarter
from fake_docker import container
from test_restarter_docker import EXPECTED_RESTART_SERVICES


def _send(server, raw: bytes, timeout: float = 60.0) -> tuple[int, dict, bytes]:
    """Send raw bytes, read until the server closes, return (status, headers, body)."""
    host, port = server.server_address[:2]
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(raw)
        data = b""
        while chunk := sock.recv(65536):
            data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split(" ", 2)[1])
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return status, headers, body


def _request(server, method: str, target: str, headers: str = "", body: bytes = b""):
    raw = f"{method} {target} HTTP/1.1\r\nHost: restarter\r\n{headers}\r\n".encode() + body
    return _send(server, raw)


def _json(status_headers_body) -> tuple[int, dict]:
    status, headers, body = status_headers_body
    assert headers["content-type"] == "application/json"
    assert headers["connection"] == "close"
    return status, json.loads(body)


def _post_restart(server, target: str = "/restart", headers: str = "", body: bytes = b""):
    length = f"Content-Length: {len(body)}\r\n"
    return _json(_request(server, "POST", target, length + headers, body))


def _docker_trace(daemon) -> list[tuple[str, str]]:
    return [(r.method, r.raw_target) for r in daemon.requests]


@pytest.fixture
def stocked(daemon):
    daemon.containers = [container(name) for name in EXPECTED_RESTART_SERVICES] + [
        container("postgres"),
        container("auth", project="other"),
    ]
    return daemon


def test_post_restart_answers_the_result_shape(surface, stocked):
    status, payload = _post_restart(surface)

    assert status == 200
    assert payload == {"restarted": sorted(EXPECTED_RESTART_SERVICES), "errors": []}


@pytest.mark.parametrize(
    ("target", "headers", "body"),
    [
        ("/restart?service=postgres", "", b""),
        ("/restart?filters=%7B%22label%22%3A%5B%5D%7D&all=1&t=0", "", b""),
        ("/restart?project=other", "", b""),
        ("/restart", "Content-Type: application/json\r\n", b'{"services":["postgres"]}'),
        ("/restart", "", b'{"project":"other","t":0}'),
        ("/restart", "X-Service: postgres\r\nX-Compose-Project: other\r\n", b""),
        ("/restart", "Authorization: Bearer x\r\nCookie: project=other\r\n", b"x" * 4096),
    ],
    ids=[
        "query-service",
        "query-filters",
        "query-project",
        "json-body",
        "body-project",
        "headers",
        "auth-cookie-and-full-body",
    ],
)
def test_no_request_parameter_reaches_docker(surface, stocked, target, headers, body):
    baseline = _post_restart(surface)
    baseline_trace = _docker_trace(stocked)
    stocked.requests.clear()

    answer = _post_restart(surface, target, headers, body)

    assert answer == baseline
    assert _docker_trace(stocked) == baseline_trace


@pytest.mark.parametrize(
    "target",
    [
        "/restart/",
        "/restart/postgres",
        "/restartx",
        "/Restart",
        "/%72estart",
        "/./restart",
        "/",
        "/health/x",
        "/containers/json",
        "/v1.44/containers/json",
        "http://restarter/other",
    ],
)
def test_any_other_path_is_404_and_reaches_no_docker_call(surface, stocked, target):
    for method in ("GET", "POST"):
        status, payload = _json(_request(surface, method, target, "Content-Length: 0\r\n"))
        assert (status, payload) == (404, {"detail": "Not found"})
    assert stocked.requests == []


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE", "PATCH", "OPTIONS", "FOO", "TRACE"])
def test_another_method_on_restart_is_405(surface, stocked, method):
    status, headers, body = _request(surface, method, "/restart", "Content-Length: 0\r\n")

    assert status == 405
    assert headers["allow"] == "POST"
    assert json.loads(body) == {"detail": "Method not allowed"}
    assert stocked.requests == []


def test_head_is_405_with_no_body(surface, stocked):
    status, headers, body = _request(surface, "HEAD", "/restart")

    assert (status, headers["allow"], headers["content-type"], body) == (
        405,
        "POST",
        "application/json",
        b"",
    )
    assert stocked.requests == []


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "FOO"])
def test_another_method_on_health_is_405(surface, stocked, method):
    status, headers, _ = _request(surface, method, "/health", "Content-Length: 0\r\n")

    assert (status, headers["allow"]) == (405, "GET")
    assert stocked.requests == []


def test_health_answers_without_asking_docker(surface, stocked):
    assert _json(_request(surface, "GET", "/health")) == (
        200,
        {"status": "ok", "service": "restarter"},
    )
    assert stocked.requests == []


@pytest.mark.parametrize(
    ("headers", "body", "expected"),
    [
        (
            "Transfer-Encoding: chunked\r\n",
            b"0\r\n\r\n",
            (400, "Transfer-Encoding is not accepted"),
        ),
        ("Content-Length: 4097\r\n", b"x" * 4097, (413, "Request body too large")),
        ("Content-Length: 99999999999\r\n", b"", (400, "Content-Length must be one whole number")),
        ("Content-Length: -1\r\n", b"", (400, "Content-Length must be one whole number")),
        ("Content-Length: 1, 1\r\n", b"x", (400, "Content-Length must be one whole number")),
        (
            "Content-Length: 1\r\nContent-Length: 1\r\n",
            b"x",
            (400, "Content-Length must be one whole number"),
        ),
        ("Content-Length: ²\r\n", b"xx", (400, "Content-Length must be one whole number")),
    ],
    ids=["chunked", "too-large", "too-many-digits", "negative", "list", "duplicate", "not-ascii"],
)
def test_a_body_framing_the_server_does_not_take_is_refused(
    surface, stocked, headers, body, expected
):
    raw = f"POST /restart HTTP/1.1\r\nHost: restarter\r\n{headers}\r\n".encode("utf-8") + body
    status, payload = _json(_send(surface, raw))

    assert (status, payload) == (expected[0], {"detail": expected[1]})
    assert stocked.requests == []


def test_an_overlong_request_line_is_414_json(surface, stocked):
    raw = b"POST /restart?" + b"a" * 70000 + b" HTTP/1.1\r\n\r\n"
    assert _json(_send(surface, raw)) == (414, {"detail": "Request-URI Too Long"})
    assert stocked.requests == []


def test_too_many_headers_is_431_json(surface, stocked):
    headers = "".join(f"X-H{i}: v\r\n" for i in range(restarter.MAX_REQUEST_HEADERS + 1))
    status, payload = _json(_request(surface, "POST", "/restart", headers))

    assert (status, payload) == (431, {"detail": "Request Header Fields Too Large"})
    assert stocked.requests == []


def test_garbage_is_400_json(surface, stocked):
    raw = b"\x16\x03\x01 nonsense NOT-HTTP\r\n\r\n"
    assert _json(_send(surface, raw)) == (400, {"detail": "Bad Request"})
    assert stocked.requests == []


def test_an_http_0_9_request_line_still_gets_a_status_line_and_json(surface, stocked):
    assert _json(_send(surface, b"GET /health\r\n\r\n")) == (
        200,
        {"status": "ok", "service": "restarter"},
    )
    assert _json(_send(surface, b"GET /restart\r\n\r\n"))[0] == 405
    assert stocked.requests == []


def test_leading_slashes_collapse_to_the_same_parameterless_call(surface, stocked):
    # http.server reduces a leading `//` to one slash before routing; the request is
    # still the one parameterless restart, with the same Docker calls as `/restart`.
    baseline = _post_restart(surface)
    baseline_trace = _docker_trace(stocked)
    stocked.requests.clear()

    assert _post_restart(surface, "//restart") == baseline
    assert _docker_trace(stocked) == baseline_trace


def test_a_client_that_sends_nothing_is_dropped_and_others_are_served(
    surface, stocked, monkeypatch
):
    monkeypatch.setattr(restarter.RestarterHandler, "timeout", 0.3)
    host, port = surface.server_address[:2]
    with socket.create_connection((host, port), timeout=5) as idle:
        assert _post_restart(surface)[0] == 200
        started = time.monotonic()
        assert idle.recv(1) == b""
        assert time.monotonic() - started < 3


def test_single_flight_under_twenty_concurrent_calls(surface, stocked):
    # One restart pass at a time (decision Q6). The daemon holds the list answer until
    # the other nineteen callers have their 409, so the overlap is certain on any
    # number of cores, then releases it; exactly one list and one pass are recorded.
    callers = 20
    stocked.list_gate = threading.Event()
    barrier = threading.Barrier(callers)
    answers: list = []
    answers_lock = threading.Lock()

    def call():
        barrier.wait()
        answer = _post_restart(surface)
        with answers_lock:
            answers.append(answer)

    threads = [threading.Thread(target=call) for _ in range(callers)]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        with answers_lock:
            if len(answers) == callers - 1:
                break
        time.sleep(0.01)
    assert stocked.list_entered.is_set()
    stocked.list_gate.set()
    for thread in threads:
        thread.join(60)

    statuses = sorted(status for status, _ in answers)
    assert statuses == [200] + [409] * (callers - 1)
    for status, payload in answers:
        if status == 409:
            assert payload == {"detail": "A restart is already in progress"}
        else:
            assert payload == {"restarted": sorted(EXPECTED_RESTART_SERVICES), "errors": []}
    assert len(stocked.list_calls()) == 1
    assert len(stocked.restarted_ids()) == len(EXPECTED_RESTART_SERVICES)

    # The lock is released once the pass ends: the next call runs a second pass.
    assert _post_restart(surface)[0] == 200
    assert len(stocked.list_calls()) == 2


def test_the_lock_is_released_after_a_failed_pass(surface, stocked):
    stocked.list_status = 500
    assert _post_restart(surface) == (
        200,
        {"restarted": [], "errors": ["Cannot list containers: DockerAPIError"]},
    )
    stocked.list_status = 200
    assert _post_restart(surface)[1]["errors"] == []


def test_the_lock_is_released_after_an_unexpected_failure(surface, stocked, monkeypatch):
    def boom(project, socket_path):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(restarter, "run_restart", boom)
    assert _post_restart(surface) == (500, {"detail": "Internal Server Error"})
    monkeypatch.undo()
    assert _post_restart(surface)[0] == 200


def test_an_unset_project_is_refused_over_http_with_no_docker_request(
    surface, stocked, monkeypatch
):
    monkeypatch.delenv(restarter.PROJECT_ENV)

    assert _post_restart(surface) == (
        200,
        {"restarted": [], "errors": [restarter.PROJECT_UNSET_ERROR]},
    )
    assert stocked.requests == []
