"""load_config answers every path state the way the services' loader does (#1451).

The config service must keep serving its page whatever config.json holds, so the
operator can repair it there: each content or path that is not a readable JSON
object is {} with one WARNING naming the path and the reason (an exception's class,
never its text, which for a UnicodeDecodeError quotes a byte of the file). The file
is read as UTF-8 whatever the locale, and a FIFO is refused before any read.
"""

import json
import logging
import os
import threading

import pytest
from app import config_store
from app.config_store import load_config

_UNUSABLE_CONTENTS = [
    pytest.param(b"{broken", "JSONDecodeError", id="unparseable"),
    pytest.param(b"", "JSONDecodeError", id="empty-file"),
    pytest.param(b'{"LOG_LEVEL": "\xff"}', "UnicodeDecodeError", id="not-utf8"),
    pytest.param(b"[" * 100_000 + b"]" * 100_000, "RecursionError", id="nested-too-deep"),
    pytest.param(b'{"LOG_LEVEL": ' + b"9" * 5000 + b"}", "ValueError", id="int-over-digit-limit"),
    pytest.param(b"[1, 2]", "not a JSON object: list", id="list"),
    pytest.param(b"null", "not a JSON object: NoneType", id="null"),
    pytest.param(b"true", "not a JSON object: bool", id="true"),
]


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "app.config_store"]


@pytest.mark.parametrize(("content", "reason"), _UNUSABLE_CONTENTS)
def test_load_config_ignores_an_unusable_file_naming_only_the_reason(
    tmp_config_dir, caplog, content, reason
):
    path = os.path.join(tmp_config_dir, "config.json")
    with open(path, "wb") as f:
        f.write(content)
    caplog.set_level(logging.WARNING, logger="app.config_store")

    assert load_config() == {}
    assert _warnings(caplog) == [f"Ignoring unusable config file {path} ({reason})"]


def _directory(path):
    os.mkdir(path)


def _fifo(path):
    os.mkfifo(path)


def _dangling_link(path):
    os.symlink(path + ".missing", path)


@pytest.mark.parametrize(
    ("make_state", "reason"),
    [
        pytest.param(_directory, "not a regular file: directory", id="directory"),
        pytest.param(_fifo, "not a regular file: FIFO", id="fifo"),
        pytest.param(_dangling_link, "FileNotFoundError", id="dangling-link"),
    ],
)
def test_load_config_refuses_a_path_that_is_not_a_regular_file_without_blocking(
    tmp_config_dir, caplog, make_state, reason
):
    path = os.path.join(tmp_config_dir, "config.json")
    make_state(path)
    caplog.set_level(logging.WARNING, logger="app.config_store")
    result = {}
    thread = threading.Thread(target=lambda: result.update(value=load_config()), daemon=True)
    thread.start()
    thread.join(5)
    assert not thread.is_alive(), "load_config blocked"
    assert result["value"] == {}
    assert _warnings(caplog) == [f"Ignoring unusable config file {path} ({reason})"]


def test_load_config_logs_nothing_for_a_missing_file(tmp_config_dir, caplog):
    caplog.set_level(logging.DEBUG, logger="app.config_store")
    assert load_config() == {}
    assert _warnings(caplog) == []


def test_load_config_reads_utf8_whatever_the_locale(tmp_config_dir, monkeypatch):
    path = os.path.join(tmp_config_dir, "config.json")
    with open(path, "wb") as f:
        f.write(json.dumps({"LOG_LEVEL": "café"}, ensure_ascii=False).encode())
    real_open = open

    def ascii_default_open(file, mode="r", *args, encoding=None, **kwargs):
        # Stand in for a locale whose default encoding is ASCII.
        return real_open(file, mode, *args, encoding=encoding or "ascii", **kwargs)

    monkeypatch.setattr("builtins.open", ascii_default_open)
    assert config_store.load_config() == {"LOG_LEVEL": "café"}
