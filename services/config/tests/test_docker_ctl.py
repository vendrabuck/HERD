"""Tests for app.docker_ctl.restart_services using a fake Docker client."""

import socket
import sys
import types
from unittest.mock import MagicMock

import pytest
from app.docker_ctl import SKIP_SERVICES, restart_services


class _FakeContainer:
    def __init__(self, service_name: str, fail: bool = False, project: str = "herd"):
        self.labels = {
            "com.docker.compose.service": service_name,
            "com.docker.compose.project": project,
        }
        self.fail = fail
        self.restart_called = False
        self.id = "fake-id-" + service_name

    def restart(self, timeout: int = 30) -> None:
        self.restart_called = True
        if self.fail:
            raise RuntimeError("kaboom")


def _install_fake_docker(monkeypatch, containers, from_env_exc=None, list_exc=None, get_exc=None):
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
        if get_exc is not None:
            client.containers.get.side_effect = get_exc
        else:
            # By default, return a container if the container_id matches one of our fakes
            def mock_get(container_id):
                for c in containers:
                    if c.id == container_id:
                        return c
                # If not found, raise NotFound (like Docker SDK)
                # We'll create a simple NotFound exception
                class NotFound(Exception):
                    pass
                raise NotFound(f"No such container: {container_id}")

            client.containers.get = mock_get  # type: ignore[attr-defined]
        fake_docker.from_env = lambda: client  # type: ignore[attr-defined]

    # Add an errors submodule for compatibility for docker.errors attribute so that `from docker.errors import NotFound` works
    class _FakeErrors:
        class NotFound(Exception):
            pass
    fake_docker.errors = _FakeErrors

    monkeypatch.setitem(sys.modules, "docker", fake_docker)


def test_restart_services_restarts_non_skipped(monkeypatch):
    # Set hostname to simulate container ID of the config service
    monkeypatch.setattr(socket, "gethostname", lambda: "fake-id-config")
    # Include the config service container in the list (it will be skipped anyway)
    config_container = _FakeContainer("config", project="herd")
    auth = _FakeContainer("auth")
    inventory = _FakeContainer("inventory")
    skipped = _FakeContainer("postgres")
    _install_fake_docker(
        monkeypatch,
        [config_container, auth, inventory, skipped],
    )

    result = restart_services()

    assert sorted(result["restarted"]) == ["auth", "inventory"]
    assert result["errors"] == []
    assert auth.restart_called is True
    assert inventory.restart_called is True
    assert skipped.restart_called is False
    # config service should be skipped
    assert config_container.restart_called is False


def test_restart_services_skips_known_services(monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: "fake-id-config")
    config_container = _FakeContainer("config", project="herd")
    containers = [_FakeContainer(name, project="herd") for name in SKIP_SERVICES]
    # Prepend the config container so it's in the list
    containers.insert(0, config_container)
    _install_fake_docker(monkeypatch, containers)

    result = restart_services()
    assert result["restarted"] == []
    assert result["errors"] == []


def test_restart_services_collects_errors(monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: "fake-id-config")
    config_container = _FakeContainer("config", project="herd")
    working = _FakeContainer("auth")
    broken = _FakeContainer("inventory", fail=True)
    _install_fake_docker(
        monkeypatch,
        [config_container, working, broken],
    )

    result = restart_services()
    assert result["restarted"] == ["auth"]
    assert len(result["errors"]) == 1
    assert "inventory" in result["errors"][0]
    assert "kaboom" in result["errors"][0]
    assert config_container.restart_called is False


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


def test_restart_services_returns_error_when_cannot_get_project(monkeypatch):
    """If the project label cannot be determined, fail closed."""
    monkeypatch.setattr(socket, "gethostname", lambda: "fake-id-config")
    # Create a container without the project label
    class _FakeContainerNoProject:
        def __init__(self):
            self.labels = {"com.docker.compose.service": "config"}
            self.id = "fake-id-config"

    def mock_get(container_id):
        return _FakeContainerNoProject()

    _install_fake_docker(
        monkeypatch,
        [],
        get_exc=None,  # We'll override the get method to return our special container
    )
    # We need to adjust the mock to return our container without project
    # Instead of using the default mock_get, we'll set a side effect
    import sys
    import types
    fake_docker = sys.modules["docker"]
    client = fake_docker.from_env()
    client.containers.get.side_effect = lambda container_id: _FakeContainerNoProject()

    result = restart_services()
    assert result["restarted"] == []
    assert any("Cannot determine project" in err for err in result["errors"])


def test_restart_services_returns_error_when_self_container_not_found(monkeypatch):
    """If the container for the hostname cannot be found, fail closed."""
    monkeypatch.setattr(socket, "gethostname", lambda: "nonexistent-id")
    _install_fake_docker(
        monkeypatch,
        [],  # No containers at all
    )
    # The get call will raise NotFound due to our default mock_get
    result = restart_services()
    assert result["restarted"] == []
    assert any("Failed to determine current project" in err for err in result["errors"])


@pytest.mark.parametrize("name", sorted(SKIP_SERVICES))
def test_skip_services_contains_expected_names(name):
    assert name in SKIP_SERVICES
