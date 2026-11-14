"""A data directory the non-root config service cannot write (issue #1326).

The config image runs as `appuser`; a herd-config volume an earlier root-run image
created holds root-owned files. The contract pinned here: startup logs ONE error line
naming the directory, the uid, and the one-time chown command, skips the first-boot
bootstrap (which writes), and keeps serving reads; a write the directory refuses is
503 with a fixed detail, never a 500 carrying a path or a traceback.

Permissions are real (chmod on the test's temporary directory), so these tests skip
when run as root, where every access check passes.
"""

from __future__ import annotations

import logging
import os
import stat
from contextlib import contextmanager

import pytest
from app import config_store
from app.main import app, lifespan
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.skipif(os.geteuid() == 0, reason="root passes every access check")

REQUIRED_ENV = {
    "POSTGRES_USER": "herd",
    "POSTGRES_PASSWORD": "secret",
    "POSTGRES_DB": "herddb",
    "AUTH_SECRET_KEY": "a-long-random-test-secret-value-0123456789",
    "INTERNAL_API_TOKEN": "tok123",
}
DETAIL = "The config data directory is not writable; the config service log names the one-time fix"


def _expected_remedy(path: str) -> str:
    uid, gid = os.getuid(), os.getgid()
    return (
        f"config data directory {path} is not writable by uid {uid}; run: docker compose "
        f"run --rm --no-deps --user root config chown -R {uid}:{gid} {path}"
    )


@contextmanager
def _read_only(*paths: str):
    """Make each path read-only (a directory keeps search permission), then restore."""
    saved = [(p, stat.S_IMODE(os.stat(p).st_mode)) for p in paths]
    try:
        for path, _ in saved:
            os.chmod(path, 0o555 if os.path.isdir(path) else 0o444)
        yield
    finally:
        for path, mode in reversed(saved):
            os.chmod(path, mode)


@pytest.fixture
def async_client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _error_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]


def test_the_remedy_names_the_directory_the_uid_and_the_command(tmp_config_dir):
    assert config_store.data_dir_remedy() == _expected_remedy(tmp_config_dir)
    assert config_store.DATA_DIR_NOT_WRITABLE_DETAIL == DETAIL


def test_a_writable_directory_is_writable(tmp_config_dir):
    assert config_store.data_dir_writable() is True


def test_a_read_only_directory_is_not_writable(tmp_config_dir):
    with _read_only(tmp_config_dir):
        assert config_store.data_dir_writable() is False


@pytest.mark.parametrize("name", ["config.json", "config_auth.json", "config.bootstrapped"])
def test_one_read_only_file_makes_the_directory_not_writable(tmp_config_dir, name):
    path = os.path.join(tmp_config_dir, name)
    with open(path, "w") as f:
        f.write("{}")
    with _read_only(path):
        assert config_store.data_dir_writable() is False


async def test_startup_logs_one_error_line_and_skips_the_bootstrap(
    tmp_config_dir, monkeypatch, caplog
):
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    caplog.set_level(logging.INFO)

    with _read_only(tmp_config_dir):
        async with lifespan(app):
            pass

    assert _error_lines(caplog) == [_expected_remedy(tmp_config_dir)]
    assert not os.path.exists(config_store.CONFIG_FILE)


async def test_startup_on_a_writable_directory_logs_no_error_and_bootstraps(
    tmp_config_dir, monkeypatch, caplog
):
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    caplog.set_level(logging.INFO)

    async with lifespan(app):
        pass

    assert _error_lines(caplog) == []
    assert os.path.exists(config_store.CONFIG_FILE)


async def test_reads_are_served_from_a_read_only_directory(tmp_config_dir, async_client):
    config_store.load_auth()  # the auth file exists, as on a volume from the old image
    with open(config_store.CONFIG_FILE, "w") as f:
        f.write("{}")

    with _read_only(config_store.AUTH_FILE, config_store.CONFIG_FILE, tmp_config_dir):
        status = await async_client.get("/status")
        schema = await async_client.get("/schema")
        login = await async_client.post("/login", json={"password": "test-config-pass"})
        token = login.json()["token"]
        settings = await async_client.get("/settings", headers={"Authorization": f"Bearer {token}"})

    assert (status.status_code, status.json()) == (
        200,
        {"configured": True, "password_changed": True},
    )
    assert (schema.status_code, login.status_code, settings.status_code) == (200, 200, 200)


async def test_a_refused_write_is_503_with_the_fixed_detail(tmp_config_dir, async_client, caplog):
    config_store.load_auth()
    login = await async_client.post("/login", json={"password": "test-config-pass"})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    caplog.set_level(logging.ERROR)

    with _read_only(config_store.AUTH_FILE, tmp_config_dir):
        save = await async_client.put("/settings", json={"values": REQUIRED_ENV}, headers=headers)
        change = await async_client.post(
            "/change-password", json={"new_password": "another-pass-1"}, headers=headers
        )

    for response in (save, change):
        assert (response.status_code, response.json()) == (503, {"detail": DETAIL})
        assert tmp_config_dir not in response.text
    assert _error_lines(caplog) == [f"{_expected_remedy(tmp_config_dir)} (PermissionError)"] * 2
    assert not os.path.exists(config_store.CONFIG_FILE)


async def test_a_first_login_that_cannot_write_the_auth_file_is_503(tmp_config_dir, async_client):
    with _read_only(tmp_config_dir):
        response = await async_client.post("/login", json={"password": "test-config-pass"})

    assert (response.status_code, response.json()) == (503, {"detail": DETAIL})
