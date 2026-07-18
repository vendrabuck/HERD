import json
import os

import pytest
from app.config_store import (
    ConfigAuthError,
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

# The known config-page password the conftest seeds via CONFIG_ADMIN_PASSWORD.
# An operator-set password is treated as rotated (write surface unlocked).
CFG_PASSWORD = "test-config-pass"
# The old hardcoded default that must no longer be accepted (issue #256).
OLD_DEFAULT_PASSWORD = "admin123!"


@pytest.fixture
def async_client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


# -- config_store unit tests --


class TestConfigStore:
    def test_default_password_verification(self):
        assert verify_password(CFG_PASSWORD) is True

    def test_wrong_password_rejected(self):
        assert verify_password("wrong-password") is False

    def test_change_password(self):
        change_password("newpass123")
        assert verify_password("newpass123") is True
        assert verify_password(CFG_PASSWORD) is False

    def test_password_changed_flag(self, monkeypatch):
        # With no operator-set password the seed is random and unrotated, so the
        # flag starts False and flips True on change (issue #256).
        monkeypatch.delenv("CONFIG_ADMIN_PASSWORD", raising=False)
        assert is_password_changed() is False
        change_password("newpass123")
        assert is_password_changed() is True

    def test_load_auth_creates_file(self, tmp_config_dir):
        auth = load_auth()
        assert "password_hash" in auth
        # CONFIG_ADMIN_PASSWORD is set by the conftest, so the seed counts as
        # operator-chosen (rotated).
        assert auth["password_changed"] is True
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


class TestCorruptAuthFile:
    """A corrupt config_auth.json must fail closed (deny login, keep /status up)
    instead of crashing with an unhandled JSONDecodeError."""

    @staticmethod
    def _corrupt_auth(tmp_config_dir):
        auth_path = os.path.join(tmp_config_dir, "config_auth.json")
        with open(auth_path, "w") as f:
            f.write("{bad")
        return auth_path

    def test_load_auth_raises_on_corrupt(self, tmp_config_dir):
        self._corrupt_auth(tmp_config_dir)
        with pytest.raises(ConfigAuthError):
            load_auth()

    def test_verify_password_fails_closed(self, tmp_config_dir):
        self._corrupt_auth(tmp_config_dir)
        # Must not raise; denies login.
        assert verify_password("anything") is False

    def test_is_password_changed_safe_default(self, tmp_config_dir):
        self._corrupt_auth(tmp_config_dir)
        # Must not raise; keeps the public /status endpoint up.
        assert is_password_changed() is False

    def test_default_password_not_regenerated_on_corrupt(self, tmp_config_dir):
        # Recovery is operator-driven, not silent: the corrupt file is left in
        # place (no default regenerated) so the admin password is not reset.
        auth_path = self._corrupt_auth(tmp_config_dir)
        assert verify_password(CFG_PASSWORD) is False
        with open(auth_path) as f:
            assert f.read() == "{bad"


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
        # Operator password set via conftest => reported rotated.
        assert data["password_changed"] is True

    @pytest.mark.asyncio
    async def test_status_configured(self, async_client, tmp_config_dir):
        config_path = os.path.join(tmp_config_dir, "config.json")
        with open(config_path, "w") as f:
            json.dump({"POSTGRES_USER": "test"}, f)
        resp = await async_client.get("/status")
        assert resp.json()["configured"] is True

    @pytest.mark.asyncio
    async def test_status_with_corrupt_auth_file(self, async_client, tmp_config_dir):
        # A corrupt config_auth.json must not 500 the public /status endpoint;
        # password_changed falls back to the safe default. configured reflects
        # is_configured(), which is unaffected by the auth file.
        auth_path = os.path.join(tmp_config_dir, "config_auth.json")
        with open(auth_path, "w") as f:
            f.write("{bad")
        resp = await async_client.get("/status")
        assert resp.status_code == 200
        assert resp.json()["password_changed"] is False


class TestLoginEndpoint:
    @pytest.mark.asyncio
    async def test_login_success(self, async_client):
        resp = await async_client.post("/login", json={"password": CFG_PASSWORD})
        assert resp.status_code == 200
        data = resp.json()
        assert "token" in data
        # Operator password set via conftest => reported rotated.
        assert data["password_changed"] is True

    @pytest.mark.asyncio
    async def test_login_wrong_password(self, async_client):
        resp = await async_client.post("/login", json={"password": "wrong"})
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_login_with_corrupt_auth_file(self, async_client, tmp_config_dir):
        # A corrupt config_auth.json must fail closed (401), not 500.
        auth_path = os.path.join(tmp_config_dir, "config_auth.json")
        with open(auth_path, "w") as f:
            f.write("{bad")
        resp = await async_client.post("/login", json={"password": CFG_PASSWORD})
        assert resp.status_code == 401


class TestChangePasswordEndpoint:
    @pytest.mark.asyncio
    async def test_change_password(self, async_client):
        # Login first
        login_resp = await async_client.post("/login", json={"password": CFG_PASSWORD})
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
        login_resp = await async_client.post("/login", json={"password": CFG_PASSWORD})
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
        login_resp = await async_client.post("/login", json={"password": CFG_PASSWORD})
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
        resp = await client.post("/login", json={"password": CFG_PASSWORD})
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
    async def test_save_settings_resolves_masked_secret_from_env(self, async_client, monkeypatch):
        # No config.json yet, secret supplied via env: the editor shows it
        # masked and the browser round-trips "********". The save must resolve
        # the placeholder from env, never write it literally, because a saved
        # file outranks env at runtime and the placeholder would become the
        # live credential.
        monkeypatch.setenv("AUTH_SECRET_KEY", "env-secret")
        headers = await self._login(async_client)
        values = {
            "POSTGRES_USER": "herd",
            "POSTGRES_PASSWORD": "secret",
            "POSTGRES_DB": "herddb",
            "AUTH_SECRET_KEY": "********",
            "INTERNAL_API_TOKEN": "tok123",
        }
        resp = await async_client.put("/settings", json={"values": values}, headers=headers)
        assert resp.status_code == 200
        assert load_config()["AUTH_SECRET_KEY"] == "env-secret"

    @pytest.mark.asyncio
    async def test_save_settings_drops_masked_optional_secret_without_source(self, async_client):
        # A masked optional secret with no file value and no env value has
        # nothing to resolve to: the key is dropped, never stored as the
        # literal placeholder.
        headers = await self._login(async_client)
        values = {
            "POSTGRES_USER": "herd",
            "POSTGRES_PASSWORD": "secret",
            "POSTGRES_DB": "herddb",
            "AUTH_SECRET_KEY": "mysecret",
            "INTERNAL_API_TOKEN": "tok123",
            "AI_API_KEY": "********",
        }
        resp = await async_client.put("/settings", json={"values": values}, headers=headers)
        assert resp.status_code == 200
        config = load_config()
        assert "AI_API_KEY" not in config
        assert "********" not in config.values()

    @pytest.mark.asyncio
    async def test_save_settings_masked_required_secret_without_source_is_422(self, async_client):
        # A masked REQUIRED secret with no source resolves to missing and must
        # fail validation rather than persist the placeholder.
        headers = await self._login(async_client)
        values = {
            "POSTGRES_USER": "herd",
            "POSTGRES_PASSWORD": "********",
            "POSTGRES_DB": "herddb",
            "AUTH_SECRET_KEY": "mysecret",
            "INTERNAL_API_TOKEN": "tok123",
        }
        resp = await async_client.put("/settings", json={"values": values}, headers=headers)
        assert resp.status_code == 422

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
        resp = await client.post("/login", json={"password": CFG_PASSWORD})
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


# -- issue #256: no guessable default, rotate-before-write lockout --


_REQUIRED_VALUES = {
    "POSTGRES_USER": "herd",
    "POSTGRES_PASSWORD": "secret",
    "POSTGRES_DB": "herddb",
    "AUTH_SECRET_KEY": "mysecret",
    "INTERNAL_API_TOKEN": "tok123",
}

_FAKE_SEED = "rand-seed-pw-abc123def456"


def _patch_random_seed(monkeypatch):
    """Drop CONFIG_ADMIN_PASSWORD and pin the generated password to a known value
    so the random-seed branch is deterministic for the test."""
    monkeypatch.delenv("CONFIG_ADMIN_PASSWORD", raising=False)
    monkeypatch.setattr("app.config_store.secrets.token_urlsafe", lambda n=24: _FAKE_SEED)


class TestSecureSeed:
    def test_old_default_password_rejected(self, monkeypatch):
        # The historical hardcoded default must never be accepted on a fresh
        # deploy that did not opt into it.
        _patch_random_seed(monkeypatch)
        assert verify_password(OLD_DEFAULT_PASSWORD) is False

    def test_random_seed_used_and_unrotated(self, monkeypatch):
        _patch_random_seed(monkeypatch)
        assert verify_password(_FAKE_SEED) is True
        assert is_password_changed() is False

    def test_env_password_is_used_and_marks_rotated(self):
        # conftest sets CONFIG_ADMIN_PASSWORD=CFG_PASSWORD.
        assert verify_password(CFG_PASSWORD) is True
        assert is_password_changed() is True


class TestWriteSurfaceLockout:
    @pytest.mark.asyncio
    async def test_put_settings_locked_until_rotated(self, async_client, monkeypatch):
        _patch_random_seed(monkeypatch)
        login = await async_client.post("/login", json={"password": _FAKE_SEED})
        assert login.status_code == 200
        headers = {"Authorization": f"Bearer {login.json()['token']}"}

        # Unrotated seed: the write surface is locked.
        locked = await async_client.put(
            "/settings", json={"values": _REQUIRED_VALUES}, headers=headers
        )
        assert locked.status_code == 403

        # Rotating the password clears the lock.
        changed = await async_client.post(
            "/change-password", json={"new_password": "newpass123"}, headers=headers
        )
        assert changed.status_code == 200
        unlocked = await async_client.put(
            "/settings", json={"values": _REQUIRED_VALUES}, headers=headers
        )
        assert unlocked.status_code == 200

    @pytest.mark.asyncio
    async def test_apply_locked_until_rotated(self, async_client, monkeypatch):
        _patch_random_seed(monkeypatch)
        login = await async_client.post("/login", json={"password": _FAKE_SEED})
        headers = {"Authorization": f"Bearer {login.json()['token']}"}
        # The rotation gate runs before the not-configured check, so an
        # unrotated seed gets 403 regardless of configured state.
        resp = await async_client.post("/apply", headers=headers)
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_write_allowed_with_operator_password(self, async_client):
        # conftest's operator password is rotated, so the surface is open.
        login = await async_client.post("/login", json={"password": CFG_PASSWORD})
        headers = {"Authorization": f"Bearer {login.json()['token']}"}
        resp = await async_client.put(
            "/settings", json={"values": _REQUIRED_VALUES}, headers=headers
        )
        assert resp.status_code == 200
