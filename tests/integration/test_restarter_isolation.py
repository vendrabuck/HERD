"""The restarter's isolation on a running stack (issue #1326, ADR 0017).

The repo-root guard tests/unit/test_restarter_compose_guard.py pins the compose files;
this checks what Docker actually built from them, without restarting anything:

- the config container has no Docker socket and does not run as root;
- from config, the restarter answers on herd-ctl (its health route, and 404 for any
  other path), so the one link Save and Restart needs is up;
- from auth, a service on herd-net only, the name `restarter` does not resolve;
- through the gateway, no route reaches the restarter: the frontend's catch-all
  answers `/api/restarter/health` with the page, not the restarter's JSON.

It never sends `POST /restart`: that bounces the stack, and runs only in nightly.yml's
last step (tests/e2e/test_config_playwright.py, test_config_save_and_restart_gated).

Bare `docker compose exec`, so COMPOSE_PROJECT_NAME from the calling environment picks
the project, as tests/integration/conftest.py's `_psql` does.
"""

from __future__ import annotations

import json
import os
import subprocess

import httpx
import pytest

from .conftest import REPO_ROOT

GATEWAY = os.getenv("HERD_BASE_URL", "https://localhost/api").rsplit("/api", 1)[0]

_PROBE = (
    "import json, urllib.request, urllib.error\n"
    "def get(url):\n"
    "    try:\n"
    "        r = urllib.request.urlopen(url, timeout=5)\n"
    "        return r.status, json.loads(r.read())\n"
    "    except urllib.error.HTTPError as e:\n"
    "        return e.code, json.loads(e.read())\n"
    "print(json.dumps([get('http://restarter:8000/health'), get('http://restarter:8000/anything')]))\n"
)

_RESOLVE = (
    "import socket\n"
    "try:\n"
    "    socket.getaddrinfo('restarter', 8000)\n"
    "    print('RESOLVED')\n"
    "except socket.gaierror:\n"
    "    print('UNRESOLVED')\n"
)


def _exec(service: str, *argv: str) -> subprocess.CompletedProcess[str]:
    cmd = ["docker", "compose", "exec", "-T", service, *argv]
    try:
        return subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"docker compose exec not usable from this host: {exc}")


def test_config_has_no_docker_socket_and_is_not_root():
    socket_check = _exec("config", "sh", "-c", "test ! -e /var/run/docker.sock && echo ABSENT")
    assert socket_check.stdout.strip() == "ABSENT", socket_check.stderr

    uid = _exec("config", "id", "-u")
    assert uid.returncode == 0, uid.stderr
    assert uid.stdout.strip() != "0"


def test_config_reaches_the_restarter_on_herd_ctl():
    probe = _exec("config", "python", "-c", _PROBE)
    assert probe.returncode == 0, probe.stderr
    assert json.loads(probe.stdout) == [
        [200, {"status": "ok", "service": "restarter"}],
        [404, {"detail": "Not found"}],
    ]


def test_a_herd_net_only_service_cannot_resolve_the_restarter():
    probe = _exec("auth", "python", "-c", _RESOLVE)
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "UNRESOLVED"


def test_the_gateway_has_no_route_to_the_restarter():
    with httpx.Client(verify=False, timeout=15.0) as client:
        response = client.get(f"{GATEWAY}/api/restarter/health")
    is_restarter = False
    if "application/json" in response.headers.get("content-type", ""):
        try:
            body = response.json()
        except ValueError:
            body = None
        is_restarter = isinstance(body, dict) and body.get("service") == "restarter"
    assert not is_restarter, response.text[:200]
