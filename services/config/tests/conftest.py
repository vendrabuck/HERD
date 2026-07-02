import os
import tempfile

import pytest
from app.config_schema import CONFIG_SCHEMA


@pytest.fixture(autouse=True)
def tmp_config_dir(monkeypatch):
    """Use a temporary directory for config storage in all tests."""
    # Clear schema-managed env vars so tests see a clean environment by default;
    # individual tests can re-set what they need via monkeypatch.setenv.
    for field in CONFIG_SCHEMA:
        monkeypatch.delenv(field["key"], raising=False)

    # Seed a known config-page password by default so the login/write tests are
    # deterministic and the write surface is unlocked (an operator-set
    # CONFIG_ADMIN_PASSWORD counts as rotated, issue #256). Tests that exercise
    # the random-seed / locked branch clear this var themselves.
    monkeypatch.setenv("CONFIG_ADMIN_PASSWORD", "test-config-pass")

    with tempfile.TemporaryDirectory() as tmpdir:
        monkeypatch.setenv("HERD_CONFIG_DATA_DIR", tmpdir)
        # Patch the module-level variables in config_store
        import app.config_store as store

        monkeypatch.setattr(store, "DATA_DIR", tmpdir)
        monkeypatch.setattr(store, "CONFIG_FILE", os.path.join(tmpdir, "config.json"))
        monkeypatch.setattr(store, "AUTH_FILE", os.path.join(tmpdir, "config_auth.json"))
        yield tmpdir
