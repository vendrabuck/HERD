"""The last Save and Restart, kept in memory so the page can read it back (issue #1433).

`POST /apply` answers only when the whole restart has finished, and the restart itself
can cut that one request off: on a Linux Docker host each restarted container's veth
interface comes and goes, and a browser on the same host aborts every request in flight
when a host address changes (Chromium's ERR_NETWORK_CHANGED). The page then reads this
record through `GET /apply/last` to learn the outcome, and reads it again on a reload.

The record holds one run: `id` (random, new for each restart the service starts),
`state` (`none` before the first restart since this process started, `running`, then
`done`), `result` (`{restarted, errors}` once done, else None), and `finished_at` (the
UTC time the result was recorded, ISO 8601 with its `+00:00` offset, to the second, once
done, else None). A run is recorded only once `POST /apply` has passed its session,
rotation, and configured checks and is about to ask the restarter, so a refused apply
never changes it. While a run is `running`, `begin` refuses a second one, which
`POST /apply` answers as the restarter's own single-flight refusal would
(`IN_PROGRESS_ERROR`), so a second click never overwrites the record of the restart the
first click is waiting for.

Standard library only, and per process: a restart of the config container itself
forgets the record (and, without `CONFIG_SESSION_SECRET`, every session).
"""

from __future__ import annotations

import secrets
import threading
from collections.abc import Callable
from datetime import datetime, timezone

STATE_NONE = "none"
STATE_RUNNING = "running"
STATE_DONE = "done"

# Recorded when the restart call raised instead of answering, so a run never stays
# `running`; the exception itself goes to the log through the server.
RESULT_LOST_ERROR = "The restart ended without a result; check service health"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RestartRecord:
    def __init__(self, clock: Callable[[], datetime] = utc_now) -> None:
        self._lock = threading.Lock()
        self._clock = clock
        self._id: str | None = None
        self._state = STATE_NONE
        self._result: dict | None = None
        self._finished_at: str | None = None

    def begin(self) -> str | None:
        """Start recording a new run and return its id; None while another runs."""
        with self._lock:
            if self._state == STATE_RUNNING:
                return None
            self._id = secrets.token_hex(16)
            self._state = STATE_RUNNING
            self._result = None
            self._finished_at = None
            return self._id

    def finish(self, run_id: str, result: dict) -> None:
        """Record the result of run `run_id`, and when; a stale id changes nothing."""
        with self._lock:
            if run_id != self._id or self._state != STATE_RUNNING:
                return
            self._state = STATE_DONE
            self._result = {
                "restarted": list(result["restarted"]),
                "errors": list(result["errors"]),
            }
            finished = self._clock().astimezone(timezone.utc)
            self._finished_at = finished.isoformat(timespec="seconds")

    def snapshot(self) -> dict:
        """The record as `GET /apply/last` answers it."""
        with self._lock:
            result = None
            if self._result is not None:
                result = {
                    "restarted": list(self._result["restarted"]),
                    "errors": list(self._result["errors"]),
                }
            return {
                "id": self._id,
                "state": self._state,
                "result": result,
                "finished_at": self._finished_at,
            }


RECORD = RestartRecord()
