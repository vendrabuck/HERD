"""The user-profile service's settings and whatever config.json holds (#1451, OPS-SET-4).

`app.config` builds `Settings()` at import, so whatever this load does is what the
service does at start. A path that does not hold a readable JSON object is answered
by who wrote it: the first-boot bootstrap copy of the environment (config.bootstrapped
beside it) is ignored and every field resolves from the environment conftest.py sets,
exactly as with no file (OPS-SET-3); an operator's file (no marker) raises
ConfigFileUnusable, so the service does not start on environment values in place of
the operator's settings. A valid object whose POSTGRES_* parts cannot form a URL loads
either way (OPS-SET-5).
"""

import json
import os
import threading

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
]


def _directory(path):
    path.mkdir()


def _fifo(path):
    os.mkfifo(path)


def _dangling_link(path):
    path.symlink_to(path.parent / "no-such-target.json")


def _write(content):
    def make(path):
        path.write_bytes(content)

    return make


_UNUSABLE = [
    *(pytest.param(_write(p.values[0]), id=p.id) for p in _UNUSABLE_CONTENTS),
    pytest.param(_directory, id="directory"),
    pytest.param(_fifo, id="fifo"),
    pytest.param(_dangling_link, id="dangling-link"),
]


def _load_settings():
    """Settings(), or the exception it raised; a test failure if it blocks."""
    result = {}

    def target():
        try:
            result["value"] = Settings()
        except Exception as exc:  # noqa: BLE001 - returned to the caller
            result["value"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(5)
    assert not thread.is_alive(), "the settings load blocked"
    return result["value"]


def _place(tmp_path, monkeypatch, make, bootstrapped):
    config_file = tmp_path / "config.json"
    make(config_file)
    if bootstrapped:
        (tmp_path / "config.bootstrapped").write_text("marker")
    monkeypatch.setattr(config_loader, "CONFIG_FILE", str(config_file))
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    return config_file


@pytest.mark.parametrize("make", _UNUSABLE)
def test_settings_load_from_the_environment_over_an_unusable_bootstrap_copy(
    tmp_path, monkeypatch, make
):
    _place(tmp_path, monkeypatch, make, bootstrapped=True)

    settings = _load_settings()

    assert settings.database_url == "sqlite+aiosqlite:///:memory:"
    assert settings.internal_api_token == "test-internal-token"
    assert settings.log_level == "DEBUG"


@pytest.mark.parametrize("make", _UNUSABLE)
def test_settings_refuse_to_load_over_an_unusable_operator_file(tmp_path, monkeypatch, make):
    config_file = _place(tmp_path, monkeypatch, make, bootstrapped=False)

    refused = _load_settings()

    assert isinstance(refused, config_loader.ConfigFileUnusable)
    message = str(refused)
    assert message.startswith(f"HERD config file {config_file} is unusable (")
    assert message.endswith(
        "Repair the file, remove it to use the environment, or save the settings again "
        "on the config page."
    )


@pytest.mark.parametrize("bootstrapped", [True, False], ids=["marker", "no-marker"])
@pytest.mark.parametrize(
    "content",
    [
        pytest.param(
            {"POSTGRES_USER": ["u"], "POSTGRES_PASSWORD": "pw", "POSTGRES_DB": "d"}, id="list-user"
        ),
        pytest.param(
            {"POSTGRES_USER": "u", "POSTGRES_PASSWORD": "\ud800", "POSTGRES_DB": "d"},
            id="lone-surrogate-password",
        ),
        pytest.param(
            {"POSTGRES_USER": 5, "POSTGRES_PASSWORD": True, "POSTGRES_DB": "d"},
            id="number-user-true-password",
        ),
    ],
)
def test_settings_load_a_valid_file_whose_postgres_parts_form_no_url(
    tmp_path, monkeypatch, content, bootstrapped
):
    # Before #1451 these raised TypeError or UnicodeEncodeError at import.
    _place(tmp_path, monkeypatch, _write(json.dumps(content).encode()), bootstrapped)

    assert _load_settings().database_url == "sqlite+aiosqlite:///:memory:"


@pytest.mark.parametrize("bootstrapped", [True, False], ids=["marker", "no-marker"])
def test_settings_still_read_a_readable_file(tmp_path, monkeypatch, bootstrapped):
    # Control: the same path does reach the file, so the cases above prove the
    # contents were ignored or refused, not that the file was never read.
    _place(
        tmp_path, monkeypatch, _write(json.dumps({"LOG_LEVEL": "WARNING"}).encode()), bootstrapped
    )
    monkeypatch.delenv("LOG_LEVEL")

    assert _load_settings().log_level == "WARNING"
