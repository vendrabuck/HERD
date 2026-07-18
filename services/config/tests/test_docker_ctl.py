"""Tests for app.docker_ctl.restart_services using a fake Docker client."""

import sys
import types

import pytest
from app.docker_ctl import PROJECT_LABEL, SERVICE_LABEL, SKIP_SERVICES, restart_services

# The compose project the fake "own container" belongs to by default.
OWN_PROJECT = "herd"


class _FakeContainer:
    def __init__(self, service_name: str, fail: bool = False, project: str = OWN_PROJECT):
        self.labels = {SERVICE_LABEL: service_name, PROJECT_LABEL: project}
        self.fail = fail
        self.restart_called = False

    def restart(self, timeout: int = 30) -> None:
        self.restart_called = True
        if self.fail:
            raise RuntimeError("kaboom")


class _FakeContainersAPI:
    """Emulates the two docker SDK calls restart_services makes.

    list() applies the same AND-semantics as the real daemon's label filters
    (bare label = presence, key=value = equality) so the project-scoping tests
    are behavioral, not assertions on the filter argument. get() serves the
    self-lookup restart_services does to find its own project label.
    """

    def __init__(self, containers, me, list_exc=None, get_exc=None):
        self._containers = containers
        self._me = me
        self._list_exc = list_exc
        self._get_exc = get_exc

    def list(self, filters=None):
        if self._list_exc is not None:
            raise self._list_exc
        wanted = (filters or {}).get("label", [])
        if isinstance(wanted, str):
            wanted = [wanted]
        matched = []
        for container in self._containers:
            for f in wanted:
                if "=" in f:
                    key, value = f.split("=", 1)
                    if container.labels.get(key) != value:
                        break
                elif f not in container.labels:
                    break
            else:
                matched.append(container)
        return matched

    def get(self, ident):
        if self._get_exc is not None:
            raise self._get_exc
        if self._me is None:
            raise RuntimeError(f"No such container: {ident}")
        return self._me


class _FakeClient:
    def __init__(self, containers_api):
        self.containers = containers_api


def _install_fake_docker(
    monkeypatch,
    containers,
    from_env_exc=None,
    list_exc=None,
    me=_FakeContainer("config"),
    get_exc=None,
):
    """Install a stub `docker` module that serves the provided containers."""

    fake_docker = types.ModuleType("docker")

    if from_env_exc is not None:

        def _from_env_raises():
            raise from_env_exc

        fake_docker.from_env = _from_env_raises  # type: ignore[attr-defined]
    else:
        api = _FakeContainersAPI(containers, me=me, list_exc=list_exc, get_exc=get_exc)
        client = _FakeClient(api)
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


def test_restart_services_scopes_to_own_project(monkeypatch):
    # Issue #373: an unscoped label filter matched every compose project on
    # the host. Containers of other projects must not be touched, including
    # ones whose service name collides with SKIP_SERVICES or with HERD names.
    ours = _FakeContainer("auth")
    other_api = _FakeContainer("api", project="reportportal")
    other_auth = _FakeContainer("auth", project="reportportal")
    other_skiplike = _FakeContainer("postgres", project="somedb")
    _install_fake_docker(monkeypatch, [ours, other_api, other_auth, other_skiplike])

    result = restart_services()

    assert result["restarted"] == ["auth"]
    assert result["errors"] == []
    assert ours.restart_called is True
    assert other_api.restart_called is False
    assert other_auth.restart_called is False
    assert other_skiplike.restart_called is False


def test_restart_services_fails_closed_when_self_lookup_fails(monkeypatch):
    # Cannot resolve our own container (not in a container, custom hostname):
    # never fall back to an unscoped restart.
    auth = _FakeContainer("auth")
    _install_fake_docker(monkeypatch, [auth], get_exc=RuntimeError("no such container"))

    result = restart_services()

    assert result["restarted"] == []
    assert any("compose project" in err for err in result["errors"])
    assert auth.restart_called is False


def test_restart_services_fails_closed_when_own_project_label_missing(monkeypatch):
    # Own container found but carries no compose project label (e.g. the
    # config service run outside compose): same fail-closed behavior.
    me = _FakeContainer("config")
    del me.labels[PROJECT_LABEL]
    auth = _FakeContainer("auth")
    _install_fake_docker(monkeypatch, [auth], me=me)

    result = restart_services()

    assert result["restarted"] == []
    assert any("compose project" in err for err in result["errors"])
    assert auth.restart_called is False


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
