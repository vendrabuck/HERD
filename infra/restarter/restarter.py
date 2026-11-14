"""HERD restarter: the one HERD process that holds the Docker socket (issue #1326, ADR 0017).

The config service's Save and Restart asks this process, over the internal `herd-ctl`
network, to restart the services that read `config.json`. Its whole API:

- `POST /restart`: no parameters. The query string, the body, and every header are
  ignored; nothing in the request reaches a Docker call. Answers 200
  `{"restarted": [...], "errors": [...]}`, or 409 `A restart is already in progress`
  while another restart runs (single flight).
- `GET /health`: liveness only; makes no Docker call.
- Any other path is 404 and any other method on those two paths is 405. Every
  response is JSON and closes the connection.

What it asks Docker, and nothing else (API version fixed at DOCKER_API_VERSION):

- `GET /v1.44/containers/json?filters={"label":["com.docker.compose.project=P"]}`,
  where P is HERD_COMPOSE_PROJECT from its own environment (compose sets it from
  COMPOSE_PROJECT_NAME). Unset or blank: it refuses and makes no Docker request.
- `POST /v1.44/containers/<64 hex id>/restart?t=30`, once for each listed container
  whose own labels name project P and a service in RESTART_SERVICES, in service order.

Errors name what failed and the exception class only; the exception text goes to the
log (issue #1086). Standard library only: no third-party package, no herd_common.

Limits: the Docker client reads a whole answer within one deadline per request
(LIST_DEADLINE_SECONDS, RESTART_DEADLINE_SECONDS), so a hung or trickling daemon
cannot hold the single-flight lock past WORST_CASE_SECONDS; the request side relies
on http.server's own line limits (65536 bytes per line, at most 100 headers) plus
MAX_REQUEST_HEADERS, MAX_REQUEST_BODY_BYTES, and a per-read socket timeout.
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logger = logging.getLogger("restarter")

DOCKER_SOCKET = "/var/run/docker.sock"
DOCKER_API_VERSION = "v1.44"
PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"
PROJECT_ENV = "HERD_COMPOSE_PROJECT"

# A: exactly the compose services that mount herd-config:/etc/herd:ro, the services
# that read config.json. tests/unit/test_restarter_compose_guard.py recomputes the set
# from docker-compose.yml and docker-compose.override.yml and fails on any difference.
RESTART_SERVICES = frozenset(
    {
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
    }
)

STOP_TIMEOUT_SECONDS = 30
LIST_DEADLINE_SECONDS = 10.0
RESTART_DEADLINE_SECONDS = STOP_TIMEOUT_SECONDS + 30.0
# The longest one POST /restart can take; the config service's client timeout must
# exceed it (tests/unit/test_restarter_compose_guard.py).
WORST_CASE_SECONDS = LIST_DEADLINE_SECONDS + len(RESTART_SERVICES) * RESTART_DEADLINE_SECONDS
MAX_DOCKER_RESPONSE_BYTES = 4 * 1024 * 1024

LISTEN_PORT = 8000
REQUEST_READ_TIMEOUT_SECONDS = 10
MAX_REQUEST_HEADERS = 32
MAX_REQUEST_BODY_BYTES = 4096

PROJECT_UNSET_ERROR = (
    "HERD_COMPOSE_PROJECT is unset or blank; refusing a restart that is not scoped "
    "to one compose project"
)
IN_PROGRESS_DETAIL = "A restart is already in progress"

_CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
_CHUNK_SIZE = re.compile(rb"[0-9a-fA-F]{1,8}")
_CONTENT_LENGTH = re.compile(r"[0-9]{1,10}")


class DockerAPIError(Exception):
    """Docker answered with a status the call does not accept."""


class UnexpectedDockerResponse(Exception):
    """Docker's answer was malformed, truncated, too large, or of the wrong shape."""


class UnexpectedContainerId(Exception):
    """A listed container id is not 64 lowercase hex characters."""


# -- Docker client: one request per connection, one deadline per request --


def _connect(socket_path: str, deadline_seconds: float) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(deadline_seconds)
        sock.connect(socket_path)
    except BaseException:
        sock.close()
        raise
    return sock


def _dechunk(data: bytes) -> bytes | None:
    """Decode a chunked body; None while incomplete."""
    body = bytearray()
    pos = 0
    while True:
        line_end = data.find(b"\r\n", pos)
        if line_end == -1:
            return None
        size_text = data[pos:line_end].split(b";", 1)[0].strip()
        if not _CHUNK_SIZE.fullmatch(size_text):
            raise UnexpectedDockerResponse("bad chunk size line")
        size = int(size_text, 16)
        pos = line_end + 2
        if size == 0:
            return bytes(body)
        if len(data) < pos + size + 2:
            return None
        if data[pos + size : pos + size + 2] != b"\r\n":
            raise UnexpectedDockerResponse("chunk not followed by CRLF")
        body += data[pos : pos + size]
        pos += size + 2


def _parse_response(data: bytes, at_eof: bool) -> tuple[int, bytes] | None:
    """Return (status, body) once `data` holds a whole answer; None while incomplete."""
    head_end = data.find(b"\r\n\r\n")
    if head_end == -1:
        if at_eof:
            raise UnexpectedDockerResponse("connection closed inside the response head")
        return None
    lines = data[:head_end].decode("latin-1").split("\r\n")
    parts = lines[0].split(" ", 2)
    if (
        len(parts) < 2
        or not parts[0].startswith("HTTP/1.")
        or not re.fullmatch(r"[0-9]{3}", parts[1])
    ):
        raise UnexpectedDockerResponse("bad status line")
    status = int(parts[1])
    headers = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if not sep:
            raise UnexpectedDockerResponse("bad header line")
        headers[name.strip().lower()] = value.strip()
    rest = data[head_end + 4 :]
    if status in (204, 304) or 100 <= status < 200:
        return status, b""
    if "transfer-encoding" in headers:
        if headers["transfer-encoding"].lower() != "chunked":
            raise UnexpectedDockerResponse("unsupported transfer encoding")
        body = _dechunk(rest)
    elif "content-length" in headers:
        if not _CONTENT_LENGTH.fullmatch(headers["content-length"]):
            raise UnexpectedDockerResponse("bad content length")
        length = int(headers["content-length"])
        body = rest[:length] if len(rest) >= length else None
    else:
        body = rest if at_eof else None
    if body is None and at_eof:
        raise UnexpectedDockerResponse("connection closed inside the response body")
    return None if body is None else (status, body)


def _exchange(sock: socket.socket, method: str, target: str, deadline_seconds: float):
    """Send one request and read its whole answer before the deadline."""
    deadline = time.monotonic() + deadline_seconds
    request = (
        f"{method} {target} HTTP/1.1\r\nHost: docker\r\nUser-Agent: herd-restarter\r\n"
        "Content-Length: 0\r\nConnection: close\r\n\r\n"
    ).encode("ascii")
    sock.settimeout(deadline_seconds)
    sock.sendall(request)
    data = b""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"Docker did not answer within {deadline_seconds} s")
        sock.settimeout(remaining)
        chunk = sock.recv(65536)
        data += chunk
        if len(data) > MAX_DOCKER_RESPONSE_BYTES:
            raise UnexpectedDockerResponse(f"answer over {MAX_DOCKER_RESPONSE_BYTES} bytes")
        parsed = _parse_response(data, at_eof=not chunk)
        if parsed is not None:
            return parsed


def _call(socket_path: str, method: str, target: str, deadline_seconds: float):
    sock = _connect(socket_path, deadline_seconds)
    try:
        return _exchange(sock, method, target, deadline_seconds)
    finally:
        sock.close()


# -- The restart --


def run_restart(project: str | None, socket_path: str = DOCKER_SOCKET) -> dict:
    """Restart RESTART_SERVICES within compose project `project`; never raises."""
    result: dict = {"restarted": [], "errors": []}
    if project is None or not project.strip():
        logger.error(PROJECT_UNSET_ERROR)
        result["errors"].append(PROJECT_UNSET_ERROR)
        return result

    filters = json.dumps({"label": [f"{PROJECT_LABEL}={project}"]}, separators=(",", ":"))
    list_target = (
        f"/{DOCKER_API_VERSION}/containers/json?{urllib.parse.urlencode({'filters': filters})}"
    )
    try:
        sock = _connect(socket_path, LIST_DEADLINE_SECONDS)
    except Exception as exc:
        result["errors"].append(f"Cannot connect to Docker: {type(exc).__name__}")
        logger.error("Cannot connect to Docker: %r", exc)
        return result
    try:
        try:
            status, body = _exchange(sock, "GET", list_target, LIST_DEADLINE_SECONDS)
        finally:
            sock.close()
        if status != 200:
            raise DockerAPIError(f"HTTP {status}: {body[:200]!r}")
        listed = json.loads(body)
        if not isinstance(listed, list):
            raise UnexpectedDockerResponse("the container list is not a JSON array")
    except Exception as exc:
        result["errors"].append(f"Cannot list containers: {type(exc).__name__}")
        logger.error("Cannot list containers: %r", exc)
        return result

    targets = []
    for item in listed:
        labels = item.get("Labels") if isinstance(item, dict) else None
        if not isinstance(labels, dict) or labels.get(PROJECT_LABEL) != project:
            continue
        service = labels.get(SERVICE_LABEL)
        if isinstance(service, str) and service in RESTART_SERVICES:
            targets.append((service, item.get("Id")))
    targets.sort(key=lambda target: (target[0], str(target[1])))

    for service, container_id in targets:
        try:
            if not isinstance(container_id, str) or not _CONTAINER_ID.fullmatch(container_id):
                raise UnexpectedContainerId(repr(container_id)[:100])
            target = (
                f"/{DOCKER_API_VERSION}/containers/{container_id}/restart?t={STOP_TIMEOUT_SECONDS}"
            )
            status, body = _call(socket_path, "POST", target, RESTART_DEADLINE_SECONDS)
            if not 200 <= status < 300:
                raise DockerAPIError(f"HTTP {status}: {body[:200]!r}")
            result["restarted"].append(service)
            logger.info("Restarted service: %s", service)
        except Exception as exc:
            result["errors"].append(f"Failed to restart {service}: {type(exc).__name__}")
            logger.error("Failed to restart %s: %r", service, exc)
    return result


# -- The HTTP surface --


class RestarterServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, socket_path: str = DOCKER_SOCKET):
        super().__init__(address, RestarterHandler)
        self.socket_path = socket_path
        self.restart_lock = threading.Lock()


class RestarterHandler(BaseHTTPRequestHandler):
    server_version = "herd-restarter"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = REQUEST_READ_TIMEOUT_SECONDS

    def __getattr__(self, name: str):
        # Every method, standard or not, goes through one dispatcher, so an unknown
        # method is 404 or 405 like any other, never http.server's 501.
        if name.startswith("do_"):
            return self._dispatch
        raise AttributeError(name)

    def log_message(self, format: str, *args) -> None:
        logger.info("%s %s", self.address_string(), format % args)

    def send_error(self, code, message=None, explain=None):
        self.close_connection = True
        self._reply(code, {"detail": self.responses.get(code, ("Error",))[0]})

    def _reply(self, status: int, payload: dict, headers: dict | None = None) -> None:
        self.close_connection = True
        if self.request_version == "HTTP/0.9":
            # http.server writes no status line or headers for HTTP/0.9, and it keeps
            # that default for a request line it could not parse; every answer here
            # carries both.
            self.request_version = "HTTP/1.0"
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _dispatch(self) -> None:
        if len(self.headers) > MAX_REQUEST_HEADERS:
            return self.send_error(431)
        if self.headers.get("Transfer-Encoding") is not None:
            return self._reply(400, {"detail": "Transfer-Encoding is not accepted"})
        lengths = self.headers.get_all("Content-Length") or []
        if len(lengths) > 1 or (lengths and not _CONTENT_LENGTH.fullmatch(lengths[0].strip())):
            return self._reply(400, {"detail": "Content-Length must be one whole number"})
        length = int(lengths[0].strip()) if lengths else 0
        if length > MAX_REQUEST_BODY_BYTES:
            return self._reply(413, {"detail": "Request body too large"})
        if length:
            self.rfile.read(length)  # drained and ignored

        route = _ROUTES.get(self.path.split("?", 1)[0])
        if route is None:
            return self._reply(404, {"detail": "Not found"})
        method, handler = route
        if self.command != method:
            return self._reply(405, {"detail": "Method not allowed"}, {"Allow": method})
        handler(self)

    def _health(self) -> None:
        self._reply(200, {"status": "ok", "service": "restarter"})

    def _restart(self) -> None:
        lock = self.server.restart_lock
        if not lock.acquire(blocking=False):
            return self._reply(409, {"detail": IN_PROGRESS_DETAIL})
        try:
            result = run_restart(os.environ.get(PROJECT_ENV), self.server.socket_path)
        except Exception as exc:  # run_restart never raises; this is the last line
            logger.error("Restart failed unexpectedly: %r", exc)
            result = None
        finally:
            lock.release()
        if result is None:
            return self._reply(500, {"detail": "Internal Server Error"})
        self._reply(200, result)


_ROUTES = {
    "/restart": ("POST", RestarterHandler._restart),
    "/health": ("GET", RestarterHandler._health),
}


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    project = os.environ.get(PROJECT_ENV)
    if project is None or not project.strip():
        logger.error(PROJECT_UNSET_ERROR)
    server = RestarterServer(("0.0.0.0", LISTEN_PORT))
    logger.info("Listening on port %d for project %r", LISTEN_PORT, project)
    server.serve_forever()


if __name__ == "__main__":
    main()
