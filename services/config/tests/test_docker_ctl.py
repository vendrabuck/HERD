"""Tests for app.docker_ctl.restart_services using a fake Docker client."""

import sys
import types
from unittest.mock import MagicMock

import pytest
from app.docker_ctl import SKIP_SERVICES, restart_services


class _FakeContainer:
    def __init__(self, service_name: str, fail: bool = False):
        self.labels = {"com.docker.compose.service": service_name}
        self.fail = fail
        self.restart_called = False

    def restart(self, timeout: int = 30) -> None:
        self.restart_called = True
        if self.fail:
            raise RuntimeError("kaboom")


def _install_fake_docker(monkeypatch, containers, from_env_exc=None, list_exc=None):
    """Install a stub `docker` module that returns the provided containers."""

    fake_docker = types.ModuleType("docker")

    if from_env_exc is not None:

        def _from_env_raises():
            raise from_env_exc

        fake_docker.from_env = _from_env_raises  # type: ignore[attr-defined]
    else:
        client = MagicMock()
        if list_exc is not None:
            client.containers.list.side_effect = list_exc
        else:
            client.containers.list.return_value = containers
        fake_docker.from_env = lambda: client  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "docker", fake_docker)


def test_restart_services_restarts_non_skipped(monkeypatch):
    auth = _FakeContainer("auth")
    inventory = _FakeContainer("inventory")
    skipped = _FakeContainer("postgres")
    _install_fake_docker(monkeypatch, [auth, inventory, skipped])

    result = restart_services()

    assert sorted(result["restarted"]) == ["auth", "inventory"]
    assert result["errors"] == []
    assert auth.restart_called is True
    assert inventory.restart_called is True
    assert skipped.restart_called is False


def test_restart_services_skips_known_services(monkeypatch):
    # Every container belongs to SKIP_SERVICES, nothing should be restarted.
    containers = [_FakeContainer(name) for name in SKIP_SERVICES]
    _install_fake_docker(monkeypatch, containers)

    result = restart_services()
    assert result["restarted"] == []
    assert result["errors"] == []


def test_restart_services_collects_errors(monkeypatch):
    working = _FakeContainer("auth")
    broken = _FakeContainer("inventory", fail=True)
    _install_fake_docker(monkeypatch, [working, broken])

    result = restart_services()
    assert result["restarted"] == ["auth"]
    assert len(result["errors"]) == 1
    assert "inventory" in result["errors"][0]
    assert "kaboom" in result["errors"][0]


def test_restart_services_returns_error_when_docker_sdk_missing(monkeypatch):
    # Pretend `import docker` fails.
    monkeypatch.setitem(sys.modules, "docker", None)
    try:
        result = restart_services()
    finally:
        monkeypatch.delitem(sys.modules, "docker", raising=False)
    assert result["restarted"] == []
    assert "Docker SDK not installed" in result["errors"][0]


def test_restart_services_returns_error_when_from_env_fails(monkeypatch):
    _install_fake_docker(monkeypatch, [], from_env_exc=RuntimeError("no socket"))

    result = restart_services()
    assert result["restarted"] == []
    assert any("Cannot connect to Docker" in err for err in result["errors"])


def test_restart_services_returns_error_when_list_fails(monkeypatch):
    _install_fake_docker(monkeypatch, [], list_exc=RuntimeError("daemon busy"))

    result = restart_services()
    assert result["restarted"] == []
    assert any("Cannot list containers" in err for err in result["errors"])


@pytest.mark.parametrize("name", sorted(SKIP_SERVICES))
def test_skip_services_contains_expected_names(name):
    assert name in SKIP_SERVICES
