"""Fixtures for the restarter's tests: the fake Docker daemon and the real HTTP surface."""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

# The restarter is one module beside this directory, not an installed package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import restarter  # noqa: E402
from fake_docker import PROJECT, start_daemon  # noqa: E402


@pytest.fixture
def daemon(tmp_path):
    fake, server = start_daemon(str(tmp_path / "docker.sock"))
    yield fake
    if fake.list_gate is not None:
        fake.list_gate.set()
    server.shutdown()
    server.server_close()


@pytest.fixture
def surface(daemon, monkeypatch):
    """The real restarter HTTP server on an ephemeral loopback port, using the fake daemon."""
    monkeypatch.setenv(restarter.PROJECT_ENV, PROJECT)
    server = restarter.RestarterServer(("127.0.0.1", 0), socket_path=daemon.socket_path)
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
