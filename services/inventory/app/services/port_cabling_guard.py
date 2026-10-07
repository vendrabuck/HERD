"""Port DELETE and port rename guard (issue #1023).

The invariant: a port is never deleted or renamed while a cabling `Connection`
row names it. Cabling identifies a port by NAME only (`port_a`, `port_b`), with
no reference to inventory's port id and no cross-schema foreign key, so a
deleted or renamed port would leave every cable that names it pointing at a
port that no longer exists, and pathfinding and wiring would keep using it.

One upstream question: cabling's `GET /connections/internal/by-port` answers
how many connections name (device_id, port_name), plus a sorted, capped id
sample. A cabled port is refused with `409 {"error": "port_cabled",
"connection_count": N, "connection_ids": [sorted sample]}`, the port-level
twin of the device DELETE guard's `device_cabled` (issue #940). The admin
removes or updates the cables first; there is no cascade and no force flag.
Cabling unreachable, erroring, or answering with an unparseable body fails
CLOSED with `503 "Could not verify port is not cabled"`: an unverifiable check
must block a destructive write.
"""

import logging
import uuid

import httpx
from fastapi import HTTPException
from herd_common.internal_client import InternalTokenAuth, call_service

from app.config import settings

logger = logging.getLogger(__name__)

PORT_UNVERIFIABLE_DETAIL = "Could not verify port is not cabled"


def _unverifiable() -> HTTPException:
    return HTTPException(status_code=503, detail=PORT_UNVERIFIABLE_DETAIL)


async def find_connections_naming_port(
    device_id: uuid.UUID, port_name: str
) -> tuple[int, list[str]]:
    """The true count and a sorted id sample of connections naming the port.

    Raises HTTPException(503) on a transport error, a missing internal token, a
    non-200 response, or a body that lacks or mistypes `connection_count`
    (non-negative int) or `connection_ids` (list). A cabling build without the
    route answers 404, which reads as unverifiable, never as "not cabled".
    """
    try:
        resp = await call_service(
            settings.cabling_service_url.rstrip("/"),
            "GET",
            "/connections/internal/by-port",
            auth=InternalTokenAuth(token=settings.internal_api_token),
            params={"device_id": str(device_id), "port_name": port_name},
            timeout=5.0,
        )
    except (httpx.HTTPError, RuntimeError) as exc:
        logger.error(
            "cabling service unreachable while checking device %s port for cables: %s",
            device_id,
            exc,
        )
        raise _unverifiable() from exc

    if resp.status_code != 200:
        logger.error(
            "cabling service returned %s while checking device %s port for cables",
            resp.status_code,
            device_id,
        )
        raise _unverifiable()

    try:
        body = resp.json()
        count = body["connection_count"]
        conn_ids = body["connection_ids"]
        if not isinstance(conn_ids, list):
            raise TypeError("connection_ids must be a list")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise TypeError("connection_count must be a non-negative int")
        return count, [str(c) for c in conn_ids]
    except (ValueError, KeyError, TypeError) as exc:
        logger.error(
            "cabling service returned an unparseable by-port body for device %s", device_id
        )
        raise _unverifiable() from exc


async def assert_port_uncabled(device_id: uuid.UUID, port_name: str) -> None:
    """Return None when no connection names the port, else raise 409
    `port_cabled` (or 503 when cabling cannot answer; see the module docstring)."""
    count, conn_ids = await find_connections_naming_port(device_id, port_name)
    if count > 0:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "port_cabled",
                "connection_count": count,
                "connection_ids": sorted(conn_ids),
            },
        )
