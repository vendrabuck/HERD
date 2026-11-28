"""The admin re-drive of pinned release rows through the gateway (issue #1199).

`POST /execution/reservations/{id}/wiring/redrive-pinned-releases` against a running
stack: routed through Traefik to execution, gated by execution's own `require_admin`
(a user's token answers 403, no token 401), and refused with 409 for a reservation whose
wiring is not frozen (here a reservation id execution holds no wiring state for, so
nothing is read or driven). The drive itself, which needs a switch whose driver package
was broken at teardown and fixed afterwards, is covered by the unit and live-Postgres
suites (`services/execution/tests/test_wiring_pinned_release_redrive.py`,
`test_wiring_retry_claim_race_live_pg.py`); no stack pattern stages a package that
breaks and is then fixed.

Requires a running stack; live-gated at review.
"""

from __future__ import annotations

import uuid

import httpx
import pytest

pytestmark = pytest.mark.asyncio

NOT_ENDED_DETAIL = (
    "Reservation wiring is not frozen; pinned release rows are re-driven only after "
    "the reservation has ended"
)


def _path(reservation_id: str) -> str:
    return f"/execution/reservations/{reservation_id}/wiring/redrive-pinned-releases"


async def test_an_admin_is_refused_with_409_for_a_reservation_that_has_not_ended(admin_client):
    resp = await admin_client.post(_path(str(uuid.uuid4())))
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == NOT_ENDED_DETAIL


async def test_a_user_is_refused_with_403(user_client):
    resp = await user_client.post(_path(str(uuid.uuid4())))
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "Admin or superadmin role required"


async def test_no_token_is_refused_with_401(base_url):
    async with httpx.AsyncClient(base_url=base_url, verify=False, timeout=30.0) as client:
        resp = await client.post(_path(str(uuid.uuid4())))
    assert resp.status_code == 401, resp.text
