"""Playwright e2e test for bulk Cancel and Release on the Reservations page (issue #843).

ReservationsPage.tsx gained a checkbox per row, a select-all for the current
page, and a selection bar with Cancel selected and Release selected. There is
no bulk endpoint: the page fans out over the per-id cancel and release calls
and the backend's own status compare-and-swap decides each row. Every
assertion that matters is read back through the API after the UI action, not
taken from the UI's own acknowledgment.

Reservations are booked through the API as the admin (so the admin owns them,
which the page's owner-only rule requires), with a per-run suffix on each
purpose because a cancelled row stays in the list and is matched exactly. The
pending ones sit 200 to 230 days out, in non-overlapping windows, so nothing
on the shared stack conflicts. They stay well under the 400 days that
test_reservations_sort_playwright.py books at: a cancelled row stays in the
list, and one starting later than that test's pair would sort above it and
break its first-row assertion. The release test needs a reservation that is
ACTIVE now, hence a free device; it skips when none is available (the
unseeded first pass skips by design, the seeded pass has devices). The
cross-owner case books as the seeded non-admin user1 and skips without it.

The page's sort preference is never touched here. Cleanup cancels whatever is
still live, even on failure.
"""

import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from playwright.sync_api import expect

from .conftest import HOST_BASE_URL, log_cleanup_failure, pw_api, pw_login

WAIT_MS = 15000
LIVE_STATUSES = ("PENDING", "PENDING_PROVISION", "ACTIVE")


def _available_devices(page) -> list[dict]:
    resp = pw_api(page, "GET", "/inventory/devices?limit=100&dut_only=true", allow_errors=True)
    if resp.status_code != 200:
        pytest.skip(f"cannot list devices: {resp.status_code}")
    payload = resp.json()
    items = payload.get("items", payload) if isinstance(payload, dict) else payload
    available = [d for d in items if d.get("status") == "AVAILABLE" and d.get("exclusive", True)]
    if not available:
        pytest.skip("no available exclusive device to reserve")
    return available


def _book(page, created: list, device_id: str, purpose: str, start: datetime) -> dict:
    body = {
        "device_ids": [device_id],
        "purpose": purpose,
        "start_time": start.isoformat(),
        "end_time": (start + timedelta(minutes=30)).isoformat(),
    }
    resp = pw_api(page, "POST", "/reservations/", json=body, allow_errors=True)
    if resp.status_code != 201:
        pytest.skip(f"reservation create failed: {resp.status_code} {resp.text}")
    reservation = resp.json()
    created.append(reservation)
    return reservation


def _status(page, reservation: dict) -> str:
    return pw_api(page, "GET", f"/reservations/{reservation['id']}").json()["status"]


def _wait_status(page, reservation: dict, want: str, timeout_s: float = 30) -> str:
    deadline = time.monotonic() + timeout_s
    status = _status(page, reservation)
    while status != want and time.monotonic() < deadline:
        time.sleep(1)
        status = _status(page, reservation)
    return status


def _cleanup(page, created: list) -> None:
    for reservation in created:
        resp = pw_api(page, "DELETE", f"/reservations/{reservation['id']}", allow_errors=True)
        log_cleanup_failure("reservation", reservation["id"], resp)


def _row(page, reservation: dict):
    return page.locator("table tbody tr").filter(
        has=page.get_by_text(reservation["purpose"], exact=True)
    )


def _check(page, reservation: dict) -> None:
    box = _row(page, reservation).get_by_role("checkbox")
    box.check()
    expect(box).to_be_checked()


def _park_pointer(page) -> None:
    # react-hot-toast pauses a toast's timer while the pointer is over the
    # toaster; keep the pointer well away from the top-right stack.
    page.mouse.move(5, 400)


def _open_page(page) -> None:
    page.goto(f"{HOST_BASE_URL}/reservations")
    expect(page.get_by_role("button", name="New Reservation", exact=True)).to_be_visible(
        timeout=WAIT_MS
    )


@pytest.fixture
def pending_trio(pw_page):
    """Three PENDING reservations on one device, in far-future non-overlapping windows."""
    pw_login(pw_page)
    device_id = _available_devices(pw_page)[0]["id"]
    run = uuid.uuid4().hex[:8]
    base = datetime.now(timezone.utc) + timedelta(days=200)
    created: list[dict] = []
    try:
        trio = [
            _book(pw_page, created, device_id, f"e2e bulk {n} {run}", base + timedelta(days=n))
            for n in (1, 2, 3)
        ]
        yield trio
    finally:
        _cleanup(pw_page, created)


def test_bulk_cancel_selected_rows_only(pw_page, pending_trio):
    """Select two of three, cancel them, then the mixed case with a finished row."""
    first, second, third = pending_trio
    for reservation in pending_trio:
        assert _status(pw_page, reservation) == "PENDING"

    _open_page(pw_page)
    expect(_row(pw_page, third)).to_be_visible(timeout=WAIT_MS)

    # A checkbox click selects and does not open the detail modal.
    _check(pw_page, first)
    expect(pw_page.locator("dialog[open]")).to_have_count(0)
    _check(pw_page, second)
    bar = pw_page.get_by_role("status", name="Selection")
    expect(bar).to_contain_text("2 selected")

    pw_page.get_by_role("button", name="Cancel selected", exact=True).click()
    dialog = pw_page.locator("dialog[open]")
    expect(dialog).to_contain_text("Cancel 2 reservations?")
    expect(dialog).to_contain_text("cannot be undone")
    expect(dialog).not_to_contain_text("skipped")
    dialog.get_by_role("button", name="Cancel 2 reservations", exact=True).click()
    _park_pointer(pw_page)

    # Effect: exactly the two selected rows are CANCELLED, the third untouched.
    assert _wait_status(pw_page, first, "CANCELLED") == "CANCELLED"
    assert _wait_status(pw_page, second, "CANCELLED") == "CANCELLED"
    assert _status(pw_page, third) == "PENDING"
    # Cancelled rows leave the selection.
    expect(bar).not_to_contain_text("selected", timeout=WAIT_MS)

    # Mixed: the now-cancelled first row plus the live third one.
    _check(pw_page, first)
    _check(pw_page, third)
    expect(bar).to_contain_text("2 selected")
    pw_page.get_by_role("button", name="Cancel selected", exact=True).click()
    dialog = pw_page.locator("dialog[open]")
    expect(dialog).to_contain_text("Cancel 1 of the 2 selected reservations?")
    expect(dialog).to_contain_text("1 will be skipped: 1 already finished.")
    dialog.get_by_role("button", name="Cancel 1 reservation", exact=True).click()
    _park_pointer(pw_page)

    assert _wait_status(pw_page, third, "CANCELLED") == "CANCELLED"
    assert _status(pw_page, first) == "CANCELLED"
    assert _status(pw_page, second) == "CANCELLED"


def test_bulk_release_acts_on_active_rows_only(pw_page):
    """Release selected releases the ACTIVE row and reports the PENDING row as skipped."""
    pw_login(pw_page)
    available = _available_devices(pw_page)
    run = uuid.uuid4().hex[:8]
    created: list[dict] = []
    try:
        now = datetime.now(timezone.utc)
        active = _book(pw_page, created, available[-1]["id"], f"e2e bulk active {run}", now)
        if _wait_status(pw_page, active, "ACTIVE") != "ACTIVE":
            pytest.skip(f"reservation never became ACTIVE (status {_status(pw_page, active)})")
        pending = _book(
            pw_page,
            created,
            available[0]["id"],
            f"e2e bulk pending {run}",
            now + timedelta(days=230),
        )
        assert _status(pw_page, pending) == "PENDING"

        _open_page(pw_page)
        expect(_row(pw_page, pending)).to_be_visible(timeout=WAIT_MS)
        _check(pw_page, active)
        _check(pw_page, pending)

        pw_page.get_by_role("button", name="Release selected", exact=True).click()
        dialog = pw_page.locator("dialog[open]")
        expect(dialog).to_contain_text("Release 1 of the 2 selected reservations?")
        expect(dialog).to_contain_text("1 will be skipped: 1 not active.")
        dialog.get_by_role("button", name="Release 1 reservation", exact=True).click()
        _park_pointer(pw_page)

        # Release ends an ACTIVE reservation as COMPLETED; the PENDING row stays.
        assert _wait_status(pw_page, active, "COMPLETED") == "COMPLETED"
        assert _status(pw_page, pending) == "PENDING"
    finally:
        _cleanup(pw_page, created)


def _user_api(token: str, method: str, path: str, **kwargs):
    with httpx.Client(verify=False, timeout=30.0) as client:
        return client.request(
            method,
            f"{HOST_BASE_URL}/api{path}",
            headers={"Authorization": f"Bearer {token}"},
            **kwargs,
        )


def test_admin_bulk_cancels_another_users_reservation(pw_page):
    """An admin may cancel any reservation: it is eligible in bulk and cancelled_by records them.

    The reservation is booked through the API as a seeded, non-admin user
    (user1, from `make seed`; no account is created here). The unseeded first
    pass has no such user and skips; the seeded pass must not.
    """
    login = httpx.post(
        f"{HOST_BASE_URL}/api/auth/login",
        json={"email": "user1@herd.dev", "password": "user1user1xx"},
        verify=False,
        timeout=30.0,
    )
    if login.status_code != 200:
        pytest.skip(f"seeded user1 cannot log in: {login.status_code}")
    token = login.json()["access_token"]
    devices = _user_api(token, "GET", "/inventory/devices?limit=100&dut_only=true")
    if devices.status_code != 200:
        pytest.skip(f"user1 cannot list devices: {devices.status_code}")
    payload = devices.json()
    items = payload.get("items", payload) if isinstance(payload, dict) else payload
    free = [d for d in items if d.get("status") == "AVAILABLE" and d.get("exclusive", True)]
    if not free:
        pytest.skip("no available exclusive device visible to user1")

    pw_login(pw_page)
    admin_id = pw_api(pw_page, "GET", "/auth/me").json()["id"]
    start = datetime.now(timezone.utc) + timedelta(days=215)
    purpose = f"e2e bulk crossowner {uuid.uuid4().hex[:8]}"
    created = _user_api(
        token,
        "POST",
        "/reservations/",
        json={
            "device_ids": [free[0]["id"]],
            "purpose": purpose,
            "start_time": start.isoformat(),
            "end_time": (start + timedelta(minutes=30)).isoformat(),
        },
    )
    if created.status_code != 201:
        pytest.skip(f"reservation create failed: {created.status_code} {created.text}")
    reservation = created.json()
    assert reservation["user_id"] != admin_id
    try:
        _open_page(pw_page)
        pw_page.get_by_label("All reservations", exact=True).check()
        _check(pw_page, reservation)

        # Cancel is open to the admin; the row is not skipped.
        pw_page.get_by_role("button", name="Cancel selected", exact=True).click()
        dialog = pw_page.locator("dialog[open]")
        expect(dialog).to_contain_text("Cancel 1 reservation?")
        expect(dialog).not_to_contain_text("skipped")
        dialog.get_by_role("button", name="Cancel 1 reservation", exact=True).click()
        _park_pointer(pw_page)

        # GET by id is owner-only, so read back as user1, who owns the row.
        deadline = time.monotonic() + 30
        read_back = _user_api(token, "GET", f"/reservations/{reservation['id']}").json()
        while read_back["status"] != "CANCELLED" and time.monotonic() < deadline:
            time.sleep(1)
            read_back = _user_api(token, "GET", f"/reservations/{reservation['id']}").json()
        assert read_back["status"] == "CANCELLED"
        assert read_back["cancelled_by"] == admin_id
    finally:
        resp = pw_api(pw_page, "DELETE", f"/reservations/{reservation['id']}", allow_errors=True)
        log_cleanup_failure("reservation", reservation["id"], resp)
