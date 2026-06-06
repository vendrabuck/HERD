"""Per-command transcript helper for driver subprocesses.

Drivers call `record_command(...)` once per command-response pair to produce a
queryable transcript of what was sent to (or would have been sent to) the
device. The runner sets `HERD_TRANSCRIPT_PATH` to a JSONL file the parent
process reads back after the subprocess exits. If the env var is unset (e.g.
driver unit tests that import the helper directly), all calls are no-ops.

This module is stdlib-only so it can be imported from inside the sandboxed
subprocess, which has no access to the FastAPI app or SQLAlchemy.
"""

import json
import os
import threading

_lock = threading.Lock()


def record_command(
    command: str,
    response: str | None = None,
    duration_ms: int | None = None,
    exit_status: str = "ok",
) -> None:
    """Append one command-response row to the transcript file.

    No-op if HERD_TRANSCRIPT_PATH is unset. Exceptions are swallowed; the
    transcript is observational and must never break a driver run.
    """
    path = os.environ.get("HERD_TRANSCRIPT_PATH")
    if not path:
        return
    row = {
        "command": command,
        "response": response,
        "duration_ms": duration_ms,
        "exit_status": exit_status,
    }
    try:
        line = json.dumps(row, default=str)
        with _lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass
