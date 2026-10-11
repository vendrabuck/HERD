"""One poll over POST /reservations/{id}/wiring/retry for the layered retry tests.

The manual retry answers ONLY for rows that are FAILED when it runs
(`reattempt_reservation` loads `failed_*_for_reservation`), and reports each one's
outcome by reading the row back by id: ACTIVE is "reconnected", RELEASED is
"released" (services/execution/app/services/wiring_retry_service.py). Two things
follow for a test that clears a mock fail knob and then polls for convergence
(issue #1158, the #953 shape):

- Execution's background retry tick (`WIRING_RETRY_INTERVAL_SECONDS`, 60 s, wall
  clock from service start, not overridden in the dev stack) drives the same FAILED
  rows. While it holds a row's drive claim the manual call answers `in_progress`
  (issue #817); that is "ask again", which the poll does.
- If the tick converges the row between the knob clear and a manual call, the row is
  no longer FAILED, so the manual answer does not name it at all. On a terminal
  reservation the answer is then execution's 409 (wiring frozen, nothing
  release-direction left to retry). Polling the retry answer alone can never see
  that convergence and times out on a correct stack.

So when the retry answer names no row for the wanted identity, the poll reads
wiring-status and accepts the row the caller saw FAILED, matched BY ID, in the
status the wanted outcome stands for. A row that is still FAILED is named by the
retry answer (or, on wiring-status, is not in the settled status) and is never
accepted; a row that never existed has no id to match.

What the fallback does NOT know is how the row got there. For "reconnected" that
loses nothing: the only writers that flip a FAILED row ACTIVE run after a driver
success (record_l1_connect, record_l2_membership_active, record_route_active), and
the retry's own "reconnected" is the same status read-back. For "released" it is
weaker than the retry's word: a release-direction row can also end RELEASED through
the supersession guard, with no driver call, which the retry reports as "superseded"
and wiring-status shows as plain RELEASED. Two things cover that. A retry answer
that reports "superseded" for the caller's own row ends the poll with None, and
callers that poll for "released" also assert a SUCCESS run of the release action
for the port, which the shared apply writes before it flips the row and which
cannot exist before the knob is cleared.
"""

import asyncio

# The wiring-status row status each convergence outcome stands for. An outcome
# that is not here (still_failed, in_progress, ...) has no settled state, so it is
# only ever taken from a retry answer.
SETTLED_STATUS = {"reconnected": "ACTIVE", "released": "RELEASED"}

# The retry answers that can mean "no FAILED row is left for this identity": a 200
# whose results do not name it, and the 409 a terminal (frozen) reservation gets
# once it has no release-direction FAILED row.
_NO_FAILED_ROW_STATUSES = (200, 409)


def matching_retry_rows(status_code: int, body, matches) -> list[dict]:
    """The retry answer's result rows that `matches` accepts; empty unless 200."""
    if status_code != 200 or not isinstance(body, dict):
        return []
    return [row for row in body.get("results", []) if matches(row)]


def settled_row(wiring_status, row_id: str, outcome: str) -> dict | None:
    """The wiring-status row with id `row_id`, if it is in `outcome`'s settled status.

    None when the outcome has no settled status, the row is absent, or the row is
    in any other status (FAILED included).
    """
    wanted = SETTLED_STATUS.get(outcome)
    if wanted is None or not isinstance(wiring_status, dict):
        return None
    for conn in wiring_status.get("connections", []):
        if str(conn.get("id")) == str(row_id):
            return conn if conn.get("status") == wanted else None
    return None


async def poll_wiring_retry(
    client,
    reservation_id: str,
    matches,
    outcome: str,
    *,
    failed_row_id: str | None = None,
    timeout: float = 25.0,
    interval: float = 0.5,
):
    """POST wiring/retry until a row `matches` accepts carries `outcome`; return it.

    `matches(row)` selects the layer and identity (a port, a switch) in the retry
    results. Returns None on timeout: never a row with another outcome, so a caller
    asserting on the result cannot be satisfied by `in_progress` or by a row that
    stayed FAILED.

    `failed_row_id` turns on the convergence fallback described in the module
    docstring: the id of the row the caller already saw FAILED (a `still_failed`
    retry row, or a FAILED wiring-status row; both carry the ledger row id). When
    a retry answer names no matching row at all, wiring-status is read and that
    row is returned if it is in the settled status for `outcome` (ACTIVE for
    "reconnected", RELEASED for "released"). The returned row is then the
    wiring-status row, which has `status` but no `outcome`, so callers assert on
    `status`: a retry row reported "reconnected" has status ACTIVE and one reported
    "released" has status RELEASED, by the retry's own read-back.

    A retry answer that reports "superseded" for the row with `failed_row_id` ends
    the poll with None at once when another outcome was wanted: that row settled
    with no driver call, and from then on wiring-status would show it RELEASED like
    a driven release.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        resp = await client.post(f"/reservations/{reservation_id}/wiring/retry")
        body = None
        if resp.status_code == 200:
            body = resp.json()
        rows = matching_retry_rows(resp.status_code, body, matches)
        for row in rows:
            if row.get("outcome") == outcome:
                return row
        if failed_row_id is not None and any(
            row.get("outcome") == "superseded" and str(row.get("id")) == str(failed_row_id)
            for row in rows
        ):
            return None
        if (
            failed_row_id is not None
            and not rows
            and resp.status_code in _NO_FAILED_ROW_STATUSES
            and outcome in SETTLED_STATUS
        ):
            status_resp = await client.get(f"/reservations/{reservation_id}/wiring-status")
            if status_resp.status_code == 200:
                settled = settled_row(status_resp.json(), failed_row_id, outcome)
                if settled is not None:
                    return settled
        await asyncio.sleep(interval)
    return None
