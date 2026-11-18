"""The acl service's settings load whatever config.json holds (#1451, OPS-SET-4).

`app.config` builds `Settings()` at import, so a content that raised here would stop
the service before it served anything. Each content that is not a readable JSON
object is ignored, and every field resolves from the environment conftest.py sets,
exactly as with no file (OPS-SET-3).
"""

import json

import pytest
from app.config import Settings
from herd_common import config_loader

_UNUSABLE_CONTENTS = [
    pytest.param(b"{broken", id="unparseable"),
    pytest.param(b"", id="empty-file"),
    pytest.param(b'{"LOG_LEVEL": "\xff"}', id="not-utf8"),
    pytest.param(b"[" * 100_000 + b"]" * 100_000, id="nested-too-deep"),
    pytest.param(b'{"LOG_LEVEL": ' + b"9" * 5000 + b"}", id="int-over-digit-limit"),
    pytest.param(b"[1, 2]", id="list"),
    pytest.param(b'"LOG_LEVEL"', id="string"),
    pytest.param(b"42", id="number"),
    pytest.param(b"null", id="null"),
    pytest.param(b"true", id="true"),
    pytest.param(
        json.dumps({"POSTGRES_USER": 5, "POSTGRES_PASSWORD": "pw", "POSTGRES_DB": "herd"}).encode(),
        id="number-postgres-user",
    ),
]


@pytest.mark.parametrize("content", _UNUSABLE_CONTENTS)
def test_settings_load_from_the_environment_whatever_the_file_holds(tmp_path, monkeypatch, content):
    config_file = tmp_path / "config.json"
    config_file.write_bytes(content)
    monkeypatch.setattr(config_loader, "CONFIG_FILE", str(config_file))
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    settings = Settings()

    assert settings.database_url == "sqlite+aiosqlite:///:memory:"
    assert settings.auth_service_url == "http://test-auth:8000"
    assert settings.log_level == "DEBUG"


def test_settings_load_from_a_directory_in_place_of_the_file(tmp_path, monkeypatch):
    config_dir = tmp_path / "config.json"
    config_dir.mkdir()
    monkeypatch.setattr(config_loader, "CONFIG_FILE", str(config_dir))

    assert Settings().database_url == "sqlite+aiosqlite:///:memory:"


def test_settings_still_read_a_readable_file(tmp_path, monkeypatch):
    # Control: the same path does reach the file, so the cases above prove the
    # contents were ignored, not that the file was never read.
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"LOG_LEVEL": "WARNING"}))
    monkeypatch.setattr(config_loader, "CONFIG_FILE", str(config_file))
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    assert Settings().log_level == "WARNING"
