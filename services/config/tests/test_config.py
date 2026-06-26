import json
import os

import pytest
from app.config_store import (
    DEFAULT_PASSWORD,
    change_password,
    is_configured,
    is_password_changed,
    load_auth,
    load_config,
    load_env_values,
    save_config,
    verify_password,
)
from app.main import app
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def async_client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


# -- config_store unit tests --


class TestConfigStore:
    def test_default_password_verification(self):
        assert verify_password(DEFAULT_PASSWORD) is True

    def test_wrong_password_rejected(self):
        assert verify_password("wrong-password") is False

    def test_change_password(self):
        change_password("newpass123")
        assert verify_password("newpass123") is True
        assert verify_password(DEFAULT_PASSWORD) is False

    def test_password_changed_flag(self):
        assert is_password_changed() is False
        change_password("newpass123")
        assert is_password_changed() is True

    def test_load_auth_creates_default(self, tmp_config_dir):
        auth = load_auth()
        assert "password_hash" in auth
        assert auth["password_changed"] is False
        assert os.path.exists(os.path.join(tmp_config_dir, "config_auth.json"))

    def test_not_configured_initially(self):
        assert is_configured() is False

    def test_load_config_empty_when_no_file(self):
        assert load_config() == {}

    def test_save_config_success(self, tmp_config_dir):
        values = {
            "POSTGRES_USER": "herd",
            "POSTGRES_PASSWORD": "secret",
            "POSTGRES_DB": "herddb",
            "AUTH_SECRET_KEY": "mysecret",
            "INTERNAL_API_TOKEN": "tok123",
        }
        errors = save_config(values)
        assert errors == []
        assert is_configured() is True
        loaded = load_config()
        assert loaded["POSTGRES_USER"] == "herd"

    def test_save_config_missing_required(self):
        errors = save_config({"POSTGRES_USER": "herd"})
        assert len(errors) > 0
        assert any("POSTGRES_PASSWORD" in e for e in errors)

    def test_load_config_corrupt_returns_empty(self, tmp_config_dir):
        config_path = os.path.join(tmp_config_dir, "config.json")
        with open(config_path, "w") as f:
            f.write("{not valid json")
        # Must not raise; falls back to {} like the missing-file path.
        assert load_config() == {}

    def test_save_config_blank_required(self):
        values = {
            "POSTGRES_USER": "",
            "POSTGRES_PASSWORD": "secret",
            "POSTGRES_DB": "herddb",
            "AUTH_SECRET_KEY": "mysecret",
            "INTERNAL_API_TOKEN": "tok123",
        }
        errors = save_config(values)
        assert any("POSTGRES_USER" in e for e in errors)


# -- API endpoint tests --


class TestHealthEndpoint:
    @pytest.mark.asyncio
    async def test_health(self, async_client):
        resp = await async_client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestStatusEndpoint:
    @pytest.mark.asyncio
    async def test_status_unconfigured(self, async_client):
        resp = await async_client.get("/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["configured"] is False
        assert data["password_changed"] is False

    @pytest.mark.asyncio
    async def test_status_configured(self, async_client, tmp_config_dir):
        config_path = os.path.join(tmp_config_dir, "config.json")
        with open(config_path, "w") as f:
            json.dump({"POSTGRES_USER": "test"}, f)
        resp = await async_client.get("/status")
        assert resp.json()["configured"] is True


class TestLoginEndpoint:
    @pytest.mark.asyncio
    async def test_login_success(self, async_client):
        resp = await async_client.post("/login", json={"password": DEFAULT_PASSWORD})
        assert resp.status_code == 200
        data = resp.json()
        assert "token" in data
        assert data["password_changed"] is False

    @pytest.mark.asyncio
    async def test_login_wrong_password(self, async_client):
        resp = await async_client.post("/login", json={"password": "wrong"})
        assert resp.status_code == 401


class TestChangePasswordEndpoint:
    @pytest.mark.asyncio
    async def test_change_password(self, async_client):
        # Login first
        login_resp = await async_client.post("/login", json={"password": DEFAULT_PASSWORD})
        token = login_resp.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        resp = await async_client.post(
            "/change-password",
            json={"new_password": "newpass123"},
            headers=headers,
        )
        assert resp.status_code == 200

        # Verify new password works
        resp2 = await async_client.post("/login", json={"password": "newpass123"})
        assert resp2.status_code == 200
        assert resp2.json()["password_changed"] is True

    @pytest.mark.asyncio
    async def test_change_password_too_short(self, async_client):
        login_resp = await async_client.post("/login", json={"password": DEFAULT_PASSWORD})
        token = login_resp.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        resp = await async_client.post(
            "/change-password",
            json={"new_password": "short"},
            headers=headers,
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_change_password_too_long(self, async_client):
        login_resp = await async_client.post("/login", json={"password": DEFAULT_PASSWORD})
        token = login_resp.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        resp = await async_client.post(
            "/change-password",
            json={"new_password": "a" * 33},
            headers=headers,
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_change_password_unauthenticated(self, async_client):
        resp = await async_client.post(
            "/change-password",
            json={"new_password": "newpass123"},
        )
        assert resp.status_code in (401, 403)


class TestSchemaEndpoint:
    @pytest.mark.asyncio
    async def test_schema(self, async_client):
        resp = await async_client.get("/schema")
        assert resp.status_code == 200
        fields = resp.json()["fields"]
        assert len(fields) > 0
        keys = [f["key"] for f in fields]
        assert "POSTGRES_USER" in keys
        assert "AUTH_SECRET_KEY" in keys
        assert "LOG_LEVEL" in keys


class TestSettingsEndpoints:
    async def _login(self, client):
        resp = await client.post("/login", json={"password": DEFAULT_PASSWORD})
        return {"Authorization": f"Bearer {resp.json()['token']}"}

    @pytest.mark.asyncio
    async def test_get_settings_empty(self, async_client):
        headers = await self._login(async_client)
        resp = await async_client.get("/settings", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["values"] == {}

    @pytest.mark.asyncio
    async def test_save_and_get_settings(self, async_client):
        headers = await self._login(async_client)
        values = {
            "POSTGRES_USER": "herd",
            "POSTGRES_PASSWORD": "secret",
            "POSTGRES_DB": "herddb",
            "AUTH_SECRET_KEY": "mysecret",
            "INTERNAL_API_TOKEN": "tok123",
            "LOG_LEVEL": "INFO",
        }
        resp = await async_client.put("/settings", json={"values": values}, headers=headers)
        assert resp.status_code == 200

        # Get settings back; secrets should be redacted
        resp2 = await async_client.get("/settings", headers=headers)
        data = resp2.json()["values"]
        assert data["POSTGRES_USER"] == "herd"
        assert data["POSTGRES_PASSWORD"] == "********"
        assert data["AUTH_SECRET_KEY"] == "********"
        assert data["LOG_LEVEL"] == "INFO"

    @pytest.mark.asyncio
    async def test_save_settings_preserves_redacted_secrets(self, async_client):
        headers = await self._login(async_client)
        values = {
            "POSTGRES_USER": "herd",
            "POSTGRES_PASSWORD": "secret",
            "POSTGRES_DB": "herddb",
            "AUTH_SECRET_KEY": "mysecret",
            "INTERNAL_API_TOKEN": "tok123",
        }
        await async_client.put("/settings", json={"values": values}, headers=headers)

        # Save again with redacted values; originals should be preserved
        values2 = {
            "POSTGRES_USER": "herd",
            "POSTGRES_PASSWORD": "********",
            "POSTGRES_DB": "herddb",
            "AUTH_SECRET_KEY": "********",
            "INTERNAL_API_TOKEN": "********",
        }
        await async_client.put("/settings", json={"values": values2}, headers=headers)

        # Verify originals are still there
        config = load_config()
        assert config["POSTGRES_PASSWORD"] == "secret"
        assert config["AUTH_SECRET_KEY"] == "mysecret"

    @pytest.mark.asyncio
    async def test_save_settings_missing_required(self, async_client):
        headers = await self._login(async_client)
        resp = await async_client.put(
            "/settings", json={"values": {"POSTGRES_USER": "herd"}}, headers=headers
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_settings_unauthenticated(self, async_client):
        resp = await async_client.get("/settings")
        assert resp.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_put_settings_unauthenticated(self, async_client):
        resp = await async_client.put("/settings", json={"values": {}})
        assert resp.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_get_settings_includes_env_values_when_no_file(self, async_client, monkeypatch):
        monkeypatch.setenv("POSTGRES_USER", "env-user")
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        headers = await self._login(async_client)
        resp = await async_client.get("/settings", headers=headers)
        data = resp.json()["values"]
        assert data["POSTGRES_USER"] == "env-user"
        assert data["LOG_LEVEL"] == "DEBUG"

    @pytest.mark.asyncio
    async def test_get_settings_file_overrides_env(self, async_client, monkeypatch):
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        headers = await self._login(async_client)
        values = {
            "POSTGRES_USER": "herd",
            "POSTGRES_PASSWORD": "secret",
            "POSTGRES_DB": "herddb",
            "AUTH_SECRET_KEY": "mysecret",
            "INTERNAL_API_TOKEN": "tok123",
            "LOG_LEVEL": "INFO",
        }
        await async_client.put("/settings", json={"values": values}, headers=headers)

        resp = await async_client.get("/settings", headers=headers)
        assert resp.json()["values"]["LOG_LEVEL"] == "INFO"

    @pytest.mark.asyncio
    async def test_get_settings_redacts_env_secrets(self, async_client, monkeypatch):
        monkeypatch.setenv("AUTH_SECRET_KEY", "super-sekret")
        headers = await self._login(async_client)
        resp = await async_client.get("/settings", headers=headers)
        assert resp.json()["values"]["AUTH_SECRET_KEY"] == "********"

    @pytest.mark.asyncio
    async def test_get_settings_with_corrupt_config_file(
        self, async_client, tmp_config_dir, monkeypatch
    ):
        # A corrupt config.json on disk must not 500 the settings editor; the
        # endpoint should fall back to env/defaults instead of crashing.
        monkeypatch.setenv("POSTGRES_USER", "env-user")
        config_path = os.path.join(tmp_config_dir, "config.json")
        with open(config_path, "w") as f:
            f.write("{truncated")
        headers = await self._login(async_client)
        resp = await async_client.get("/settings", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["values"]["POSTGRES_USER"] == "env-user"


class TestLoadEnvValues:
    def test_returns_only_present_and_non_blank(self, monkeypatch):
        monkeypatch.setenv("POSTGRES_USER", "u")
        monkeypatch.setenv("POSTGRES_PASSWORD", "   ")
        assert load_env_values() == {"POSTGRES_USER": "u"}

    def test_ignores_unknown_keys(self, monkeypatch):
        monkeypatch.setenv("NOT_IN_SCHEMA", "x")
        assert "NOT_IN_SCHEMA" not in load_env_values()


class TestApplyEndpoint:
    async def _login(self, client):
        resp = await client.post("/login", json={"password": DEFAULT_PASSWORD})
        return {"Authorization": f"Bearer {resp.json()['token']}"}

    @pytest.mark.asyncio
    async def test_apply_not_configured(self, async_client):
        headers = await self._login(async_client)
        resp = await async_client.post("/apply", headers=headers)
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_apply_unauthenticated(self, async_client):
        resp = await async_client.post("/apply")
        assert resp.status_code in (401, 403)
