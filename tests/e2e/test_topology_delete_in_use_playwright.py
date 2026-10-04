"""Playwright e2e tests for the topology delete guard (issue #977).

cabling refuses DELETE /topologies/{id} with 409 `topology_in_use` while a
PENDING, PENDING_PROVISION, or ACTIVE reservation references the topology. The
Topologies page shows that refusal in plain words: on the row after a bulk
Delete selected, and in the toast after a row's own Delete. Every assertion
that matters is read back through the API after the UI action.

Each test books a FUTURE reservation (well over a year out, a random window so
parallel sessions do not collide) on the first AVAILABLE exclusive device, so
nothing activates and no wiring is driven. Cleanup cancels the reservation
first and only then deletes the topologies (the guard would refuse otherwise),
and restores the page's saved search and sort preferences (the issue #951
rule).

Device-gated: on the unseeded first e2e pass there is no device and the
fixture skips; the seeded pass must not.
"""

import random
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from playwright.sync_api import expect

from .conftest import log_cleanup_failure, pw_api, pw_login
from .test_topologies_list_playwright import (
    WAIT_MS,
    _open_page,
    _park_pointer,
    _SavedPrefs,
    _search,
)

IN_USE_ONE = "In use by 1 reservation. Cancel it or wait for it to end, then delete again."


def _available_device_id(page) -> str:
    resp = pw_api(page, "GET", "/inventory/devices?limit=100&dut_only=true", allow_errors=True)
    if resp.status_code != 200:
        pytest.skip(f"cannot list devices: {resp.status_code}")
    payload = resp.json()
    items = payload.get("items", payload) if isinstance(payload, dict) else payload
    available = [d for d in items if d.get("status") == "AVAILABLE" and d.get("exclusive", True)]
    if not available:
        pytest.skip("no available exclusive device to reserve")
    return available[0]["id"]


def _book_future(page, device_id: str, topology_id: str) -> dict:
    """A PENDING reservation on a random far-future window, retried on a clash."""
    last = None
    for _ in range(5):
        start = datetime.now(timezone.utc) + timedelta(
            days=700 + random.randint(0, 300), minutes=random.randint(0, 1440)
        )
        body = {
            "device_ids": [device_id],
            "topology_id": topology_id,
            "purpose": f"e2e topology delete guard {uuid.uuid4().hex[:8]}",
            "start_time": start.isoformat(),
            "end_time": (start + timedelta(minutes=30)).isoformat(),
        }
        last = pw_api(page, "POST", "/reservations/", json=body, allow_errors=True)
        if last.status_code == 201:
            reservation = last.json()
            assert reservation["status"] == "PENDING"
            return reservation
        if last.status_code not in (409, 422):
            break
    pytest.fail(f"reservation create failed: {last.status_code} {last.text}")


@pytest.fixture
def in_use_and_free(pw_page):
    """Two admin topologies under one per-run prefix; `used` has a live booking.

    Yields (prefix, used, free, reservation). Teardown cancels the reservation,
    then deletes whatever topology is left, then checks both are gone.
    """
    pw_login(pw_page)
    device_id = _available_device_id(pw_page)
    prefix = f"pw977-{uuid.uuid4().hex[:8]}"
    created: list[dict] = []
    reservation = None
    try:
        for suffix in ("used", "free"):
            resp = pw_api(
                pw_page, "POST", "/cabling/topologies", json={"name": f"{prefix}-{suffix}"}
            )
            created.append(resp.json())
        used, free = created
        reservation = _book_future(pw_page, device_id, used["id"])
        yield prefix, used, free, reservation
    finally:
        if reservation is not None:
            resp = pw_api(
                pw_page, "DELETE", f"/reservations/{reservation['id']}", allow_errors=True
            )
            log_cleanup_failure("reservation", reservation["id"], resp)
            status = pw_api(pw_page, "GET", f"/reservations/{reservation['id']}").json()["status"]
            assert status == "CANCELLED", status
        for topo in created:
            resp = pw_api(pw_page, "DELETE", f"/cabling/topologies/{topo['id']}", allow_errors=True)
            if resp.status_code != 404:
                log_cleanup_failure("topology", topo["id"], resp)
            gone = pw_api(pw_page, "GET", f"/cabling/topologies/{topo['id']}", allow_errors=True)
            assert gone.status_code == 404, f"topology {topo['id']} left behind"


@pytest.fixture
def admin_prefs(pw_page, in_use_and_free):
    saved = _SavedPrefs(pw_page)
    saved.reset()
    try:
        yield saved
    finally:
        saved.restore()


def _status(page, topology_id: str) -> int:
    return pw_api(page, "GET", f"/cabling/topologies/{topology_id}", allow_errors=True).status_code


def test_bulk_delete_removes_only_the_free_topology_and_explains_the_other(
    pw_page, in_use_and_free, admin_prefs
):
    prefix, used, free, reservation = in_use_and_free
    _open_page(pw_page)
    _search(pw_page, prefix)
    expect(pw_page.locator("table tbody tr")).to_have_count(2, timeout=WAIT_MS)

    for topo in (used, free):
        pw_page.get_by_role("checkbox", name=f"Select topology {topo['name']}", exact=True).check()
    pw_page.get_by_role("button", name="Delete selected", exact=True).click()
    dialog = pw_page.locator("dialog[open]")
    dialog.get_by_role("button", name="Delete 2 topologies", exact=True).click()
    _park_pointer(pw_page)

    expect(pw_page.locator("table tbody tr")).to_have_count(1, timeout=WAIT_MS)
    row = pw_page.locator("table tbody tr").first
    expect(row).to_contain_text(used["name"])
    expect(row).to_contain_text(f"Not deleted: {IN_USE_ONE}")
    expect(
        pw_page.get_by_role("checkbox", name=f"Select topology {used['name']}", exact=True)
    ).to_be_checked()
    expect(pw_page.get_by_text(f"Deleted 1, failed 1: {IN_USE_ONE}", exact=True)).to_be_visible(
        timeout=WAIT_MS
    )

    # Effect: exactly the free topology is gone; the referenced one and its
    # booking are untouched, and the server names that booking.
    assert _status(pw_page, free["id"]) == 404
    assert _status(pw_page, used["id"]) == 200
    refused = pw_api(pw_page, "DELETE", f"/cabling/topologies/{used['id']}", allow_errors=True)
    assert refused.status_code == 409
    assert refused.json()["detail"] == {
        "error": "topology_in_use",
        "reservation_ids": [reservation["id"]],
    }
    booking = pw_api(pw_page, "GET", f"/reservations/{reservation['id']}").json()
    assert booking["status"] == "PENDING"
    assert booking["topology_id"] == used["id"]


def test_row_delete_shows_the_in_use_reason_and_keeps_the_topology(
    pw_page, in_use_and_free, admin_prefs
):
    prefix, used, free, reservation = in_use_and_free
    _open_page(pw_page)
    _search(pw_page, prefix)
    expect(pw_page.locator("table tbody tr")).to_have_count(2, timeout=WAIT_MS)

    pw_page.get_by_role("button", name=f"Delete topology {used['name']}", exact=True).click()
    dialog = pw_page.locator("dialog[open]")
    expect(dialog).to_contain_text("Delete topology?")
    dialog.get_by_role("button", name="Delete", exact=True).click()
    _park_pointer(pw_page)

    expect(pw_page.get_by_text(f"Topology not deleted. {IN_USE_ONE}", exact=True)).to_be_visible(
        timeout=WAIT_MS
    )
    expect(pw_page.locator("table tbody tr")).to_have_count(2)

    # Effect: both topologies are still there and the booking is unchanged.
    assert _status(pw_page, used["id"]) == 200
    assert _status(pw_page, free["id"]) == 200
    booking = pw_api(pw_page, "GET", f"/reservations/{reservation['id']}").json()
    assert booking["status"] == "PENDING"
