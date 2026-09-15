"""Cabling-service reads used by the topology generator.

Only the batch pathfinder is here today: the generator asks cabling which
candidate device pairs the cable graph can actually connect before it commits
a proposal to concrete devices.

Fails CLOSED by design. A transport error or any non-200 raises
`CablingUnavailableError` instead of returning a partial answer, because the
only alternative reading of a missing result is "not reachable", and that
would silently turn a cabling outage into a stream of bogus
`topology_unconnectable` refusals. This is the same discipline as execution's
`_fetch_fork_intended_wires`: unreadable intent defers, it never resolves
against an empty set.
"""

import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# Cabling's own per-request cap is MAX_BATCH_PAIRS = 2000
# (services/cabling/app/schemas/pathfind.py), enforced by the request schema
# with a 422. Chunking well under it keeps each request's payload and BFS
# work small; the whole batch shares one adjacency-graph build per request.
PATHFIND_BATCH_CHUNK = 200

PATHFIND_TIMEOUT_SECONDS = 20.0


class CablingUnavailableError(Exception):
    """The cabling service could not answer, so reachability is unknown."""


async def fetch_pathfind_batch(
    user_bearer_token: str,
    pairs: list[tuple[str, str]],
) -> list[dict[str, Any]]:
    """Resolve every (source, target) device pair through cabling's batch pathfinder.

    The caller's JWT is forwarded, so device-group visibility applies exactly
    as it does everywhere else in the AI path: a pair naming a device the
    caller cannot see comes back with the per-pair `error` field set
    (issue #763), which the resolver reads as "no path" rather than as a
    distinguishable refusal.

    Returns the concatenated per-pair result objects, in request order.
    """
    if not pairs:
        return []

    headers = {"Authorization": f"Bearer {user_bearer_token}"}
    url = f"{settings.cabling_service_url.rstrip('/')}/pathfind/batch"
    results: list[dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=PATHFIND_TIMEOUT_SECONDS) as client:
        for start in range(0, len(pairs), PATHFIND_BATCH_CHUNK):
            chunk = pairs[start : start + PATHFIND_BATCH_CHUNK]
            body = {
                "pairs": [
                    {"source_device_id": source, "target_device_id": target}
                    for source, target in chunk
                ]
            }
            try:
                resp = await client.post(url, json=body, headers=headers)
            except httpx.HTTPError as e:
                logger.warning("pathfind_batch_transport_error: %s", e)
                raise CablingUnavailableError("pathfind request failed") from e
            if resp.status_code != 200:
                logger.warning("pathfind_batch_status: %s", resp.status_code)
                raise CablingUnavailableError(f"pathfind returned HTTP {resp.status_code}")
            try:
                payload = resp.json()
            except ValueError as e:
                logger.warning("pathfind_batch_bad_body")
                raise CablingUnavailableError("pathfind returned a non-JSON body") from e
            chunk_results = payload.get("results")
            if not isinstance(chunk_results, list):
                raise CablingUnavailableError("pathfind returned no results list")
            results.extend(chunk_results)

    return results
