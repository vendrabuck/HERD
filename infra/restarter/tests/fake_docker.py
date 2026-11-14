"""A fake Docker daemon for the restarter's tests.

The fake daemon is a standard-library HTTP server on a Unix socket in the test's
tmp_path that records every request it receives (method, path, query) and answers
from a list of containers. It applies the project label filter the way the real
daemon does unless `honor_filters` is off, which the invariant tests use to prove the
restarter checks each container's labels itself. conftest.py puts `infra/restarter` on
sys.path, so `restarter` imports here as it does in the tests.
"""

from __future__ import annotations

import json
import socketserver
import threading
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler

import restarter

PROJECT = "herd"


_DERIVED = object()


def container(service: str | None, project: str | None = PROJECT, cid: object = _DERIVED) -> dict:
    """A container as GET /containers/json lists it; its id is derived unless given."""
    labels = {}
    if project is not None:
        labels[restarter.PROJECT_LABEL] = project
    if service is not None:
        labels[restarter.SERVICE_LABEL] = service
    if cid is _DERIVED:
        cid = (f"{project}-{service}".encode().hex() * 8)[:64]
    return {"Id": cid, "Names": [f"/{project}-{service}-1"], "Labels": labels}


@dataclass
class Recorded:
    method: str
    path: str
    query: dict
    raw_target: str


@dataclass
class FakeDaemon:
    socket_path: str
    containers: list = field(default_factory=list)
    honor_filters: bool = True
    list_status: int = 200
    list_raw: bytes | None = None  # a raw answer replaces the whole list response
    list_chunked: bool = False
    restart_status: dict = field(default_factory=dict)  # id to status, default 204
    list_gate: threading.Event | None = None  # the list answer waits for this
    restart_hang: set = field(default_factory=set)  # ids whose restart never answers
    hang_release: threading.Event = field(default_factory=threading.Event)
    list_entered: threading.Event = field(default_factory=threading.Event)
    requests: list = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def restarted_ids(self) -> list[str]:
        return [
            r.path.split("/")[3]
            for r in self.requests
            if r.method == "POST" and r.path.endswith("/restart")
        ]

    def list_calls(self) -> list[Recorded]:
        return [r for r in self.requests if r.path.endswith("/containers/json")]


def _matches(item: object, wanted: list[str]) -> bool:
    """The daemon's label filter: `key=value` is equality, a bare `key` is presence."""
    labels = item.get("Labels") if isinstance(item, dict) else None
    labels = labels if isinstance(labels, dict) else {}
    for want in wanted:
        key, sep, value = want.partition("=")
        if (labels.get(key) != value) if sep else (key not in labels):
            return False
    return True


class _UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def _daemon_handler(daemon: FakeDaemon):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format, *args):
            pass

        def address_string(self):
            return "unix"

        def _answer(self, status: int, body: bytes = b"", chunked: bool = False):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            if chunked:
                self.send_header("Transfer-Encoding", "chunked")
            elif status != 204:
                self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            if chunked:
                for i in range(0, len(body), 7):
                    piece = body[i : i + 7]
                    self.wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
            elif status != 204:
                self.wfile.write(body)
            self.close_connection = True

        def _record(self):
            parsed = urllib.parse.urlsplit(self.path)
            with daemon.lock:
                daemon.requests.append(
                    Recorded(
                        self.command,
                        parsed.path,
                        urllib.parse.parse_qs(parsed.query, keep_blank_values=True),
                        self.path,
                    )
                )
            return parsed

        def do_GET(self):
            parsed = self._record()
            if not parsed.path.endswith("/containers/json"):
                return self._answer(404, b'{"message":"page not found"}')
            daemon.list_entered.set()
            if daemon.list_gate is not None:
                daemon.list_gate.wait(30)
            if daemon.list_raw is not None:
                self.wfile.write(daemon.list_raw)
                self.close_connection = True
                return
            listed = daemon.containers
            if daemon.honor_filters:
                filters = json.loads(urllib.parse.parse_qs(parsed.query)["filters"][0])
                wanted = filters.get("label", [])
                listed = [c for c in listed if _matches(c, wanted)]
            body = json.dumps(listed).encode()
            self._answer(daemon.list_status, body, chunked=daemon.list_chunked)

        def do_POST(self):
            parsed = self._record()
            cid = parsed.path.split("/")[3] if parsed.path.count("/") >= 4 else ""
            if cid in daemon.restart_hang:
                daemon.hang_release.wait(30)
                return
            status = daemon.restart_status.get(cid, 204)
            body = b"" if status == 204 else b'{"message":"daemon secret text /var/lib/x"}'
            self._answer(status, body)

    return Handler


def start_daemon(socket_path: str) -> tuple[FakeDaemon, _UnixHTTPServer]:
    fake = FakeDaemon(socket_path=socket_path)
    server = _UnixHTTPServer(socket_path, _daemon_handler(fake))
    threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
    return fake, server
