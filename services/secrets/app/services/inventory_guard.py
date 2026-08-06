"""Cross-service guard: does a hypervisor still reference a secret?

Issue #456: delete_secret had no reference guard, unlike inventory's
delete_hypervisor and delete_driver (both 409 while templates reference
them), so an admin could delete a secret a hypervisor still referenced. The
dangle is not cosmetic: execution's dynamic provisioning treats a missing
secret as a PermanentEventError, so every later reservation against that
hypervisor's templates fails at provision time, far from the cause.

This is the first inbound reference INTO secrets from another service's
schema, so it mirrors the shape of inventory's issue #337 reservation guard
(services/inventory/app/services/reservation_guard.py): an internal-token
reverse lookup against the owning service, failing CLOSED with 503 on a
transport error or non-200. A secret delete is destructive and rare, so an
unverifiable reference check must block, not silently let the delete
through. No force flag, per the issue #391 reasoning: the escape hatch is
re-pointing or deleting the referencing hypervisor first, which is the
correct order anyway.

Known and accepted TOCTOU window, the same class #337 accepted: a
hypervisor registering (which validates the secret exists) concurrently
with a delete whose reverse lookup ran first can still produce a dangle;
both sides are rare admin actions and the UI renders the orphaned state.

If a second service ever grows a reference to secrets, the fan-out here is
the signal to migrate to a claims registry local to this service (the
option C shape in the #456 decision writeup); the 409 contract stays the
same either way.
"""

import logging
import uuid

import httpx
from fastapi import HTTPException

from app.config import settings

logger = logging.getLogger(__name__)


async def find_hypervisors_referencing_secret(secret_id: uuid.UUID) -> list[dict]:
    """Return [{id, name}, ...] for hypervisors referencing this secret.

    Calls inventory's by-secret internal endpoint with X-Internal-Token.
    Raises HTTPException(503) on a transport error or a non-200 response
    instead of failing open; see the module docstring for why.
    """
    url = f"{settings.inventory_service_url.rstrip('/')}/hypervisors/by-secret/{secret_id}/internal"
    headers = {"X-Internal-Token": settings.internal_api_token}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        logger.error(
            "inventory service unreachable while checking secret references: %s",
            exc,
        )
        raise HTTPException(
            status_code=503,
            detail="inventory service unreachable while checking secret references",
        ) from exc

    if resp.status_code != 200:
        logger.error(
            "inventory service returned %s while checking secret references",
            resp.status_code,
        )
        raise HTTPException(
            status_code=503,
            detail="inventory service returned an error while checking secret references",
        )

    return resp.json()
