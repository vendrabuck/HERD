"""Save and Restart's one outbound call: ask the restarter to restart (ADR 0017).

The config service holds no Docker socket (issue #1326). `restart_services` makes one
parameterless `POST /restart` to the restarter on the internal `herd-ctl` network;
the restarter decides the compose project and the services to restart. Nothing from
the config request reaches that call.

The answer passed back to `POST /apply` is always `{restarted, errors}` built here:

- a 200 whose body is exactly that shape (two lists of short printable strings) is
  passed through;
- a 409 is the restarter's single-flight refusal, reported as IN_PROGRESS_ERROR;
- any other status is `The restart service answered HTTP <status>`;
- a 200 with any other body is UNEXPECTED_BODY_ERROR;
- no answer is `Cannot reach the restart service: <ExceptionClass>`.

The restarter's body is never echoed when it fails those checks, and an exception's
text goes to the log only (issue #1086). Standard library only: the config image
ships no HTTP client package.
"""

from __future__ import annotations

import http.client
import json
import logging

logger = logging.getLogger(__name__)

RESTARTER_HOST = "restarter"
RESTARTER_PORT = 8000
RESTART_PATH = "/restart"
# Above the restarter's worst case (its WORST_CASE_SECONDS, 670 s: the list plus a
# 60 s deadline for each of eleven restarts), so the restarter, not this client, is
# the one that gives up. tests/unit/test_restarter_compose_guard.py pins the order.
RESTART_TIMEOUT_SECONDS = 720.0

IN_PROGRESS_ERROR = "A restart is already in progress"
UNEXPECTED_BODY_ERROR = "The restart service answered an unexpected body"
MAX_RESPONSE_BYTES = 64 * 1024
MAX_ENTRIES = 64
MAX_ENTRY_CHARS = 200


def _error(message: str) -> dict:
    return {"restarted": [], "errors": [message]}


def _validated(body: bytes) -> dict | None:
    """The restarter's answer when it is exactly `{restarted, errors}`; else None."""
    if len(body) > MAX_RESPONSE_BYTES:
        return None
    try:
        data = json.loads(body)
    except ValueError:
        return None
    if not isinstance(data, dict) or set(data) != {"restarted", "errors"}:
        return None
    for key in ("restarted", "errors"):
        entries = data[key]
        if not isinstance(entries, list) or len(entries) > MAX_ENTRIES:
            return None
        for entry in entries:
            if not isinstance(entry, str) or not entry or len(entry) > MAX_ENTRY_CHARS:
                return None
            if not entry.isprintable():
                return None
    return {"restarted": list(data["restarted"]), "errors": list(data["errors"])}


def restart_services() -> dict:
    """Ask the restarter to restart this stack's config-reading services.

    Blocking: `POST /apply` runs it in a worker thread.
    """
    try:
        conn = http.client.HTTPConnection(
            RESTARTER_HOST, RESTARTER_PORT, timeout=RESTART_TIMEOUT_SECONDS
        )
        try:
            conn.request("POST", RESTART_PATH, body=b"")
            response = conn.getresponse()
            status = response.status
            body = response.read(MAX_RESPONSE_BYTES + 1)
        finally:
            conn.close()
    except Exception as exc:
        logger.error("Cannot reach the restart service: %r", exc)
        return _error(f"Cannot reach the restart service: {type(exc).__name__}")

    if status == 409:
        return _error(IN_PROGRESS_ERROR)
    if status != 200:
        logger.error("The restart service answered HTTP %d: %r", status, body[:500])
        return _error(f"The restart service answered HTTP {status}")
    result = _validated(body)
    if result is None:
        logger.error("The restart service answered an unexpected body: %r", body[:500])
        return _error(UNEXPECTED_BODY_ERROR)
    for name in result["restarted"]:
        logger.info("Restarted service: %s", name)
    return result
