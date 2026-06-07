"""Tests for app.services.driver_transcript.record_command.

The helper is stdlib-only and runs inside the sandboxed subprocess. It appends
one JSONL row per command to the file named by HERD_TRANSCRIPT_PATH, and is a
no-op when that env var is unset. Exceptions are swallowed (observational only).
"""

import json
import os

from app.services.driver_transcript import record_command


def _read_rows(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_record_command_noop_when_env_unset(monkeypatch, tmp_path):
    """With HERD_TRANSCRIPT_PATH unset, the call writes nothing and returns None."""
    monkeypatch.delenv("HERD_TRANSCRIPT_PATH", raising=False)
    target = tmp_path / "should_not_exist.jsonl"
    assert record_command("show version") is None
    assert not target.exists()


def test_record_command_writes_one_row(monkeypatch, tmp_path):
    path = tmp_path / "tx.jsonl"
    monkeypatch.setenv("HERD_TRANSCRIPT_PATH", str(path))

    record_command("show version", response="ok", duration_ms=12, exit_status="ok")

    rows = _read_rows(str(path))
    assert rows == [
        {
            "command": "show version",
            "response": "ok",
            "duration_ms": 12,
            "exit_status": "ok",
        }
    ]


def test_record_command_appends_multiple_rows(monkeypatch, tmp_path):
    path = tmp_path / "tx.jsonl"
    monkeypatch.setenv("HERD_TRANSCRIPT_PATH", str(path))

    record_command("cmd-a")
    record_command("cmd-b", response="b-out", exit_status="error")

    rows = _read_rows(str(path))
    assert [r["command"] for r in rows] == ["cmd-a", "cmd-b"]
    # Defaults applied to the first row.
    assert rows[0]["response"] is None
    assert rows[0]["duration_ms"] is None
    assert rows[0]["exit_status"] == "ok"
    assert rows[1]["exit_status"] == "error"


def test_record_command_defaults(monkeypatch, tmp_path):
    path = tmp_path / "tx.jsonl"
    monkeypatch.setenv("HERD_TRANSCRIPT_PATH", str(path))

    record_command("just-a-command")

    (row,) = _read_rows(str(path))
    assert row["response"] is None
    assert row["duration_ms"] is None
    assert row["exit_status"] == "ok"


def test_record_command_serializes_non_str_default(monkeypatch, tmp_path):
    """json.dumps uses default=str, so a non-serializable response coerces to a string."""
    path = tmp_path / "tx.jsonl"
    monkeypatch.setenv("HERD_TRANSCRIPT_PATH", str(path))

    class _Weird:
        def __str__(self):
            return "weird-repr"

    record_command("cmd", response=_Weird())  # type: ignore[arg-type]

    (row,) = _read_rows(str(path))
    assert row["response"] == "weird-repr"


def test_record_command_swallows_write_errors(monkeypatch):
    """A bad path must not raise: the transcript is observational, never fatal."""
    # Point at a path whose parent directory does not exist; open() raises, the
    # helper swallows it. The assertion is simply that no exception escapes.
    monkeypatch.setenv("HERD_TRANSCRIPT_PATH", os.path.join("/nonexistent_dir_xyz", "tx.jsonl"))
    assert record_command("cmd") is None
