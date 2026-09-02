"""Playwright e2e test for the admin bulk connection create flow (issue #540).

Drives the multi-connection dialog
(frontend/src/components/admin/connections/MultiConnectDialog.tsx, PR #538),
which is the DEFAULT create surface on the admin Connections page, and commits
a batch through POST /api/cabling/connections/bulk (PR #537).

The dialog had unit coverage and a one-off manual browser check before merge,
but nothing committed guarded the flow, which is what this module adds. That
gap mattered because e2e does not run in per-PR CI: PR #538 was green on every
check that actually executed and still broke a Playwright test.

Two behaviors are covered, both asserted through backend API read-back rather
than UI acknowledgment, per the standing convention in
test_connections_playwright.py:

- staging lines by click, then confirming the batch and reading every created
  cable back through the cabling API
- the partial-success path, where the batch contains a row the backend
  rejects: created lines drop out of the dialog while the rejected line stays
  staged carrying the server's reason (applyBulkResult in bulkStaging.ts)

Devices are created fresh by each test (issue #670) rather than picked as
the first pair the inventory API happens to return: on a stack that has been
through load tests or repeated e2e passes, the first pair's free-port count
depends on cabling history and can come up short, skipping the test. Two
brand-new devices with brand-new ports start with no cabling at all, so every
port is free regardless of stack history.
"""

import io
import tarfile
import time
import uuid

import pytest
from playwright.sync_api import expect

from .conftest import HOST_BASE_URL, pw_api, pw_login

# Minimum ports a candidate device must expose to be usable here. Having this
# many ports does not guarantee this many FREE ones, which is why staging also
# filters on availability and skips when a pair comes up short.
MIN_PORTS = 3

# How many lines each test stages. Small enough to stay quick and far under the
# endpoint's 200-row cap, large enough that the batch is genuinely plural.
STAGED_LINES = 3


def _driver_tarball() -> bytes:
    """A minimal no-op Management driver, enough to back a device template."""
    body = b"class Driver:\n    pass\n"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo("driver.py")
        info.size = len(body)
        tf.addfile(info, io.BytesIO(body))
    return buf.getvalue()


def _seed_fixture_devices(page, min_ports: int):
    """Create two fresh devices, each with min_ports fresh ports, via the API.

    Replaces picking the first seeded pair (issue #670): a brand-new device
    starts with zero cabling, so every port it gets is guaranteed free,
    independent of what load tests or prior e2e passes did to the shared
    stack's seeded fleet. Returns ((device_a, ports_a), (device_b, ports_b))
    plus the ids of everything created, for teardown.
    """
    suffix = uuid.uuid4().hex[:10]
    files = {"file": ("e2e-bulk-driver.tar.gz", _driver_tarball(), "application/gzip")}
    driver = pw_api(
        page,
        "POST",
        "/inventory/drivers",
        files=files,
        data={
            "name": f"e2e-pw-bulk-drv-{suffix}",
            "connection_type": "Management",
            "description": "bulk-connect e2e test driver",
        },
    ).json()

    device_template = pw_api(
        page,
        "POST",
        "/inventory/templates",
        json={
            "name": f"e2e-pw-bulk-devtmpl-{suffix}",
            "template_type": "device",
            "driver_id": driver["id"],
            "vendor": "e2e-pw-bulk",
            "model": "fixture",
            "sections": [{"name": "General", "fields": []}],
        },
    ).json()
    port_template = pw_api(
        page,
        "POST",
        "/inventory/templates",
        json={
            "name": f"e2e-pw-bulk-porttmpl-{suffix}",
            "template_type": "port",
            "sections": [{"name": "General", "fields": []}],
        },
    ).json()

    devices = []
    for label in ("a", "b"):
        device = pw_api(
            page,
            "POST",
            "/inventory/devices",
            json={
                "name": f"e2e-pw-bulk-dev-{label}-{suffix}",
                "template_id": device_template["id"],
                "topology_type": "PHYSICAL",
            },
        ).json()
        ports = pw_api(
            page,
            "POST",
            f"/inventory/devices/{device['id']}/ports/bulk",
            json={
                "name_prefix": f"e2e-{label}-",
                "starting_index": 1,
                "instances": min_ports,
                "template_id": port_template["id"],
            },
        ).json()
        devices.append((device, ports))

    ids = {
        "driver_id": driver["id"],
        "device_template_id": device_template["id"],
        "port_template_id": port_template["id"],
        "device_ids": [devices[0][0]["id"], devices[1][0]["id"]],
    }
    return devices[0], devices[1], ids


def _cleanup_fixture_devices(page, ids: dict) -> None:
    """Delete everything _seed_fixture_devices created, in dependency order."""
    for device_id in ids["device_ids"]:
        pw_api(page, "DELETE", f"/inventory/devices/{device_id}", allow_errors=True)
    pw_api(page, "DELETE", f"/inventory/templates/{ids['device_template_id']}", allow_errors=True)
    pw_api(page, "DELETE", f"/inventory/templates/{ids['port_template_id']}", allow_errors=True)
    pw_api(page, "DELETE", f"/inventory/drivers/{ids['driver_id']}", allow_errors=True)


def _select_device(page, device_name: str) -> None:
    """Type into the next unresolved device search box and pick the match.

    Both the A-side and B-side pickers render a "Search devices..." input
    until picked, at which point that input unmounts (replaced by a name plus
    a Change button), so `.first` targets the A side on the first call and the
    B side on the second. Same helper shape as the single-modal sibling test.
    """
    search_input = page.get_by_placeholder("Search devices...").first
    search_input.fill(device_name)
    page.get_by_role("button", name=device_name, exact=True).click()


def _open_multi_dialog(page, device_a, device_b):
    """Open the multi-connect dialog with both devices selected.

    Asserts on the way through that the multi dialog is the DEFAULT surface:
    no Multi/Single toggle click happens here, unlike the single-modal test
    which must flip to Single first.
    """
    page.goto(f"{HOST_BASE_URL}/admin/connections")
    expect(page.get_by_role("button", name="Create Connection")).to_be_visible()
    page.get_by_role("button", name="Create Connection").click()

    dialog = page.locator("dialog[open]")
    expect(dialog.locator("#modal-title")).to_have_text("Create multiple connections")

    _select_device(page, device_a["name"])
    _select_device(page, device_b["name"])
    return dialog


def _free_port_pairs(dialog, ports_a, ports_b, count):
    """Pick `count` genuinely free ports from each side, in render order.

    The fixture devices are fresh (issue #670), so every port on them should
    be free; this still reads the DOM's own notion of free
    (aria-disabled="false", what PortColumn renders for a cabled port) rather
    than assuming it, keeping the test in step with whatever the availability
    rules decide. The skip below is now a defensive fallback, not the
    expected path.

    MultiConnectDialog gates EVERY row's aria-disabled on
    `interactionDisabled = connectionsLoading || submitting`
    (both columns share one flag), so right after the B-side device is
    picked its connections query is still in flight and every row briefly
    reads aria-disabled="true". Waiting for the first row on each side to
    settle avoids sampling mid-flight and skipping on a false negative.
    """
    if ports_a:
        expect(dialog.get_by_test_id(f"port-row-{ports_a[0]['id']}")).to_have_attribute(
            "aria-disabled", "false"
        )
    if ports_b:
        expect(dialog.get_by_test_id(f"port-row-{ports_b[0]['id']}")).to_have_attribute(
            "aria-disabled", "false"
        )
    free_a = [p for p in ports_a if _is_free(dialog, p)]
    free_b = [p for p in ports_b if _is_free(dialog, p)]
    if len(free_a) < count or len(free_b) < count:
        pytest.skip(
            f"need {count} free ports per side, found {len(free_a)} on A and {len(free_b)} on B"
        )
    return free_a[:count], free_b[:count]


def _is_free(dialog, port) -> bool:
    """True when the dialog renders this port as selectable.

    Ports outside the virtualized window are not in the DOM at all; those are
    treated as unavailable here rather than scrolled into view, since the
    tests only need a handful from the top of each column.
    """
    row = dialog.get_by_test_id(f"port-row-{port['id']}")
    if row.count() == 0:
        return False
    return row.get_attribute("aria-disabled") == "false"


def _cleanup(page, notes):
    """Delete every connection this test created, found by its notes marker.

    Keyed on the unique per-run notes string rather than on ids collected
    during the test body: a failure BEFORE the read-back step would leave that
    id list empty while the backend had already created the cables, stranding
    them in the shared dev/gate stack. Looking them up here means cleanup does
    not depend on how far the test got.
    """
    listing = pw_api(page, "GET", "/cabling/connections", params={"skip": 0, "limit": 500}).json()[
        "items"
    ]
    for conn in listing:
        if conn.get("notes") == notes:
            pw_api(page, "DELETE", f"/cabling/connections/{conn['id']}", allow_errors=True)


def test_bulk_connection_create(pw_page):
    """Stage several lines, confirm the batch, verify every cable via the API."""
    pw_login(pw_page)
    (device_a, ports_a), (device_b, ports_b), fixture_ids = _seed_fixture_devices(
        pw_page, MIN_PORTS
    )
    notes = f"e2e-pw-bulk-{int(time.time() * 1000)}"
    created_ids = []

    try:
        dialog = _open_multi_dialog(pw_page, device_a, device_b)

        # Stage a known number of lines by clicking a free port on each side in
        # turn. The port rows arm on mousedown (the drag-versus-click
        # arbitration), and a Playwright click delivers mousedown plus mouseup,
        # so a click stages a line. Staging explicitly rather than via "Connect
        # 1:1 in order" keeps the expected count fixed: 1:1 pairs EVERY free
        # port, and each fixture device carries exactly MIN_PORTS.
        free_a, free_b = _free_port_pairs(dialog, ports_a, ports_b, STAGED_LINES)
        for port_a, port_b in zip(free_a, free_b):
            dialog.get_by_test_id(f"port-row-{port_a['id']}").click()
            dialog.get_by_test_id(f"port-row-{port_b['id']}").click()

        pw_page.fill("#multi-connect-notes", notes)
        confirm = dialog.get_by_role("button", name=f"Create {STAGED_LINES} connections")
        expect(confirm).to_be_enabled()
        confirm.click()

        # Dialog closes only when every staged line was created.
        expect(pw_page.locator("dialog[open]")).to_have_count(0)

        # --- Effect assertion via the cabling API ---
        listing = pw_api(
            pw_page,
            "GET",
            "/cabling/connections",
            params={"device_id": device_a["id"], "skip": 0, "limit": 200},
        ).json()["items"]
        matches = [c for c in listing if c.get("notes") == notes]
        created_ids = [c["id"] for c in matches]

        assert len(matches) == STAGED_LINES, (
            f"expected {STAGED_LINES} cables created, API returned {len(matches)}"
        )

        # Every created cable joins the two chosen devices, on distinct ports,
        # and carries the batch-level connection_type.
        staged_pairs = set()
        for conn in matches:
            assert {conn["device_a_id"], conn["device_b_id"]} == {device_a["id"], device_b["id"]}
            assert conn["connection_type"] == "ethernet"
            staged_pairs.add((conn["port_a"], conn["port_b"]))
        assert len(staged_pairs) == STAGED_LINES, "bulk create produced duplicate port pairs"

        # Exactly the ports clicked, in the order clicked: this is what proves
        # the click-to-connect path carried the right pairs to the backend,
        # rather than merely creating the right NUMBER of cables.
        expected_pairs = {(a["name"], b["name"]) for a, b in zip(free_a, free_b)}
        assert staged_pairs == expected_pairs

        # The page reflects the new cables without a manual reload.
        expect(pw_page.locator("tr", has_text=notes).first).to_be_visible()
    finally:
        _cleanup(pw_page, notes)
        # Baseline restored: every cable this run created is gone.
        for connection_id in created_ids:
            gone = pw_api(
                pw_page, "GET", f"/cabling/connections/{connection_id}", allow_errors=True
            )
            assert gone.status_code == 404, "cleanup left a connection behind"
        _cleanup_fixture_devices(pw_page, fixture_ids)


def test_bulk_partial_success_keeps_rejected_line_staged(pw_page):
    """A batch with one backend-rejected row keeps that line staged, with reason.

    The rejection is provoked by rewriting ONE row in flight into a self-loop
    (same device, same port on both ends), which _validate_connection_row
    refuses with 422 "Cannot connect a port to itself". That is the rejection
    the bulk path actually applies per row: port NAMES are deliberately not
    validated against inventory, so a bogus port name would be created, not
    rejected, and a bogus device id would abort the whole batch fail-closed
    rather than produce the partial success under test here.
    """
    pw_login(pw_page)
    (device_a, ports_a), (device_b, ports_b), fixture_ids = _seed_fixture_devices(
        pw_page, MIN_PORTS
    )
    notes = f"e2e-pw-bulk-partial-{int(time.time() * 1000)}"

    try:
        dialog = _open_multi_dialog(pw_page, device_a, device_b)

        # Intercept the bulk call and rewrite the FIRST row into a self-loop so
        # the backend rejects it. Routing the real request through keeps the
        # response an authentic server report, not a fixture: only the outbound
        # request is altered, and the rejection reason below is the server's.
        def corrupt_first_row(route):
            payload = route.request.post_data_json
            first = payload["items"][0]
            first["device_b_id"] = first["device_a_id"]
            first["port_b"] = first["port_a"]
            route.continue_(post_data=payload)

        pw_page.route("**/api/cabling/connections/bulk", corrupt_first_row)

        free_a, free_b = _free_port_pairs(dialog, ports_a, ports_b, STAGED_LINES)
        for port_a, port_b in zip(free_a, free_b):
            dialog.get_by_test_id(f"port-row-{port_a['id']}").click()
            dialog.get_by_test_id(f"port-row-{port_b['id']}").click()

        pw_page.fill("#multi-connect-notes", notes)
        confirm = dialog.get_by_role("button", name=f"Create {STAGED_LINES} connections")
        expect(confirm).to_be_enabled()
        confirm.click()

        # The dialog STAYS open on partial success, holding the rejected line.
        expect(pw_page.locator("dialog[open]")).to_have_count(1)
        expect(dialog.get_by_role("button", name="Create 1 connection")).to_be_enabled()

        # The review strip is collapsed by default but auto-opens on a partial
        # success (MultiConnectDialog line 502), surfacing the flagged lines
        # without the admin hunting for them; do NOT click Review here, that
        # would toggle it shut again.
        review = dialog.get_by_test_id("review-strip")
        expect(review).to_be_visible()
        # The surviving staged line carries the SERVER's reason verbatim.
        expect(review).to_contain_text("Cannot connect a port to itself")

        # --- Effect assertion: the siblings landed, the rejected row did not ---
        listing = pw_api(
            pw_page,
            "GET",
            "/cabling/connections",
            params={"device_id": device_a["id"], "skip": 0, "limit": 200},
        ).json()["items"]
        matches = [c for c in listing if c.get("notes") == notes]

        expected_created = STAGED_LINES - 1
        assert len(matches) == expected_created, (
            f"expected {expected_created} of {STAGED_LINES} rows created, "
            f"API returned {len(matches)}"
        )
        # The rejected self-loop row was never persisted.
        assert all(c["device_a_id"] != c["device_b_id"] for c in matches)
    finally:
        pw_page.unroute("**/api/cabling/connections/bulk")
        _cleanup(pw_page, notes)
        _cleanup_fixture_devices(pw_page, fixture_ids)
