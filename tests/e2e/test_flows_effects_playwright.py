"""Playwright e2e tests for four issue #388 Tier 1 flows, effect-asserted.

Covers issue #388 Tier 1 items 6, 7, 9, and 10. Each test drives a real UI
surface and, per the effect-assertion discipline established in
test_config_playwright.py, confirms the backend-observable effect via an API
read-back after every UI mutation (never the UI acknowledgment alone).

- item 6: the bulk import dialog (components/ui/BulkImportExport.tsx) for both
  devices (InventoryPage) and topologies (TopologyPage): dry-run first with a
  report-count assertion AND an API read-back proving nothing was written, then
  a real import with an API read-back proving the resource now exists.
- item 7: the device config-version cycle through the DeviceConfigSection JSON
  editor: create, view, create a second, diff the two, restore the first, then
  assert the resulting version list via the inventory API.
- item 9: reservation create through CreateReservationModal including a
  dynamic-instance row (a dynamic template + mock hypervisor recipe are seeded
  via API first), asserted ACTIVE via API, then cancelled through the detail
  modal and asserted terminal via API.
- item 10: the notification round-trip: a real reservation.created +
  reservation.cancelled pair drives two owner notifications, the bell badge is
  asserted to show an unread count, one notification is marked read through the
  panel, and the notifications API confirms that exact notification flipped to
  read.

Shared-stack discipline: every created resource is uuid-suffixed so it never
collides with seeded data or a concurrent agent, and every mutation is undone
in a finally block, cancelling reservations before deleting the devices they
hold so the inventory in-use delete guard (issue #337) does not 409. Because
the admin user is shared, item 10 asserts on THIS test's specific notifications
(matched by reservation_id) rather than a global unread count, which a
concurrent agent could move under us.
"""

import io
import json
import tarfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from playwright.sync_api import expect

from .conftest import HOST_BASE_URL, pw_login

_MOCK_HV_DIR = Path(__file__).resolve().parents[2] / "drivers" / "mock_hypervisor"


def _token(page) -> str | None:
    return page.evaluate("() => window.localStorage.getItem('access_token')")


def _api(page, method, path, **kwargs):
    """Authenticated host-side HERD API request, using the browser's own JWT."""
    allow_errors = kwargs.pop("allow_errors", False)
    token = _token(page)
    headers = kwargs.pop("headers", {}) or {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"{HOST_BASE_URL}/api{path}"
    with httpx.Client(verify=False, timeout=30.0) as client:
        resp = client.request(method, url, headers=headers, **kwargs)
    if not allow_errors:
        resp.raise_for_status()
    return resp


def _poll(fn, predicate, *, timeout: float = 30.0, interval: float = 0.5):
    """Call fn() until predicate(result) is true or timeout elapses.

    Returns the predicate-satisfying result, or None on timeout, so an assertion
    on the return value cannot silently pass against a last-known-bad snapshot.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = fn()
        if predicate(result):
            return result
        time.sleep(interval)
    return None


def _bare_driver_tarball(body: bytes = b"class Driver:\n    pass\n") -> bytes:
    """A minimal driver.py package, enough for a device template."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo("driver.py")
        info.size = len(body)
        tf.addfile(info, io.BytesIO(body))
    return buf.getvalue()


def _mock_hv_tarball() -> bytes:
    """Package the checked-in drivers/mock_hypervisor recipe into a .tar.gz."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name in ("driver.py", "driver_metadata.json"):
            tf.add(_MOCK_HV_DIR / name, arcname=name)
    return buf.getvalue()


def _seed_device_template(page, suffix: str, connection_type: str = "Management") -> dict:
    """Seed a driver + device template via API; return ids plus the template name.

    The template is a plain device template with a single "model" field, enough
    to hang a device off and (for Management) to validate config versions
    against the hardcoded connection-type schema.
    """
    files = {"file": (f"drv-{suffix}.tar.gz", _bare_driver_tarball(), "application/gzip")}
    data = {
        "name": f"pw-flows-drv-{suffix}",
        "connection_type": connection_type,
        "description": "playwright flows-effects test driver",
    }
    driver = _api(page, "POST", "/inventory/drivers", files=files, data=data).json()

    template_name = f"pw-flows-tmpl-{suffix}"
    template = _api(
        page,
        "POST",
        "/inventory/templates",
        json={
            "name": template_name,
            "template_type": "device",
            "driver_id": driver["id"],
            "vendor": "PlaywrightVendor",
            "model": "FlowsDUT",
            "sections": [
                {
                    "name": "General",
                    "fields": [{"key": "model", "label": "Model", "type": "string"}],
                }
            ],
        },
    ).json()
    return {
        "driver_id": driver["id"],
        "template_id": template["id"],
        "template_name": template_name,
    }


# ---------------------------------------------------------------------------
# item 6: bulk import dialog, devices
# ---------------------------------------------------------------------------


def test_bulk_device_import_dialog_dry_run_then_commit(pw_page):
    pw_login(pw_page)
    suffix = uuid.uuid4().hex[:8]
    device_name = f"pw-bulk-dev-{suffix}"
    seed = None
    created_device_id = None
    try:
        seed = _seed_device_template(pw_page, suffix)

        payload = {
            "resource": "devices",
            "version": 1,
            "items": [
                {
                    "name": device_name,
                    "template_name": seed["template_name"],
                    "topology_type": "PHYSICAL",
                    "status": "AVAILABLE",
                    "field_data": {"model": "bulk"},
                }
            ],
        }
        file_bytes = json.dumps(payload).encode()

        pw_page.goto(f"{HOST_BASE_URL}/inventory")
        import_btn = pw_page.get_by_role("button", name="Import", exact=True)
        expect(import_btn).to_be_visible()
        import_btn.click()

        dialog = pw_page.locator("dialog[open]")
        expect(dialog.get_by_text("Import devices")).to_be_visible()

        pw_page.set_input_files(
            "#bulk-file",
            files=[
                {
                    "name": f"devices-{suffix}.json",
                    "mimeType": "application/json",
                    "buffer": file_bytes,
                }
            ],
        )

        # --- Dry run: report shows one create, and the API proves NO write ---
        dialog.get_by_role("button", name="Dry run", exact=True).click()
        report = dialog.locator('[role="status"]')
        expect(report).to_contain_text("Dry-run preview")
        expect(report).to_contain_text("1 create")

        found = _api(
            pw_page, "GET", "/inventory/devices", params={"search": device_name, "limit": 50}
        ).json()["items"]
        assert not any(d["name"] == device_name for d in found), (
            "dry-run must not create the device"
        )

        # --- Commit: report shows one create, and the API proves it exists ---
        dialog.get_by_role("button", name="Import now", exact=True).click()
        expect(report).to_contain_text("Import result")
        expect(report).to_contain_text("1 create")

        landed = _poll(
            lambda: _api(
                pw_page,
                "GET",
                "/inventory/devices",
                params={"search": device_name, "limit": 50},
            ).json()["items"],
            lambda items: any(d["name"] == device_name for d in items),
            timeout=15.0,
        )
        assert landed is not None, "committed import never surfaced the device via the API"
        match = next(d for d in landed if d["name"] == device_name)
        created_device_id = match["id"]
        assert match["template_id"] == seed["template_id"]
        assert match["topology_type"] == "PHYSICAL"
    finally:
        if created_device_id:
            _api(pw_page, "DELETE", f"/inventory/devices/{created_device_id}", allow_errors=True)
        if seed:
            tid = seed["template_id"]
            _api(pw_page, "DELETE", f"/inventory/templates/{tid}", allow_errors=True)
            _api(pw_page, "DELETE", f"/inventory/drivers/{seed['driver_id']}", allow_errors=True)


# ---------------------------------------------------------------------------
# item 6: bulk import dialog, topologies
# ---------------------------------------------------------------------------


def test_bulk_topology_import_dialog_dry_run_then_commit(pw_page):
    pw_login(pw_page)
    suffix = uuid.uuid4().hex[:8]
    topo_name = f"pw-bulk-topo-{suffix}"
    seed = None
    device = None
    created_topology_id = None
    try:
        seed = _seed_device_template(pw_page, suffix)
        device = _api(
            pw_page,
            "POST",
            "/inventory/devices",
            json={
                "name": f"pw-bulk-topodev-{suffix}",
                "template_id": seed["template_id"],
                "topology_type": "PHYSICAL",
                "status": "AVAILABLE",
                "field_data": {"model": "bulk"},
            },
        ).json()

        # A single-node, edge-less topology: the node's device name must resolve
        # against inventory, and an edge-less canvas passes topology validation.
        payload = {
            "resource": "topologies",
            "version": 1,
            "items": [
                {
                    "name": topo_name,
                    "canvas": {
                        "nodes": [
                            {
                                "id": "n1",
                                "data": {
                                    "device": {"name": device["name"]},
                                    "label": device["name"],
                                },
                            }
                        ],
                        "edges": [],
                    },
                }
            ],
        }
        file_bytes = json.dumps(payload).encode()

        pw_page.goto(f"{HOST_BASE_URL}/topology")
        import_btn = pw_page.get_by_role("button", name="Import", exact=True)
        expect(import_btn).to_be_visible()
        import_btn.click()

        dialog = pw_page.locator("dialog[open]")
        expect(dialog.get_by_text("Import topologies")).to_be_visible()

        pw_page.set_input_files(
            "#bulk-file",
            files=[
                {
                    "name": f"topologies-{suffix}.json",
                    "mimeType": "application/json",
                    "buffer": file_bytes,
                }
            ],
        )

        def _list_topos():
            return _api(pw_page, "GET", "/cabling/topologies", params={"limit": 500}).json()[
                "items"
            ]

        # --- Dry run: report shows one create, and the API proves NO write ---
        dialog.get_by_role("button", name="Dry run", exact=True).click()
        report = dialog.locator('[role="status"]')
        expect(report).to_contain_text("Dry-run preview")
        expect(report).to_contain_text("1 create")
        assert not any(t["name"] == topo_name for t in _list_topos()), (
            "dry-run must not create the topology"
        )

        # --- Commit: report shows one create, and the API proves it exists ---
        dialog.get_by_role("button", name="Import now", exact=True).click()
        expect(report).to_contain_text("Import result")
        expect(report).to_contain_text("1 create")

        landed = _poll(
            _list_topos,
            lambda items: any(t["name"] == topo_name for t in items),
            timeout=15.0,
        )
        assert landed is not None, "committed import never surfaced the topology via the API"
        created_topology_id = next(t["id"] for t in landed if t["name"] == topo_name)
    finally:
        if created_topology_id:
            _api(pw_page, "DELETE", f"/cabling/topologies/{created_topology_id}", allow_errors=True)
        if device:
            _api(pw_page, "DELETE", f"/inventory/devices/{device['id']}", allow_errors=True)
        if seed:
            tid = seed["template_id"]
            _api(pw_page, "DELETE", f"/inventory/templates/{tid}", allow_errors=True)
            _api(pw_page, "DELETE", f"/inventory/drivers/{seed['driver_id']}", allow_errors=True)


# ---------------------------------------------------------------------------
# item 7: device config-version cycle
# ---------------------------------------------------------------------------


def test_device_config_version_cycle(pw_page):
    pw_login(pw_page)
    suffix = uuid.uuid4().hex[:8]
    seed = None
    device = None

    # Two Management configs that validate against the connection-type schema
    # (herd_common.device_config: vlan/ip/hostname/description). Optional keys
    # are omitted entirely, never sent as explicit null.
    config_a = {"vlan": 100, "hostname": f"pw-cfg-a-{suffix}", "description": "first"}
    config_b = {"vlan": 200, "hostname": f"pw-cfg-b-{suffix}", "description": "second"}

    try:
        seed = _seed_device_template(pw_page, suffix, connection_type="Management")
        device = _api(
            pw_page,
            "POST",
            "/inventory/devices",
            json={
                "name": f"pw-cfg-dev-{suffix}",
                "template_id": seed["template_id"],
                "topology_type": "PHYSICAL",
                "status": "AVAILABLE",
                "field_data": {"model": "cfg"},
            },
        ).json()
        device_id = device["id"]

        pw_page.goto(f"{HOST_BASE_URL}/inventory/{device_id}")
        expect(pw_page.get_by_text("Configuration history")).to_be_visible()

        # --- Create version 1 via the JSON editor ---
        pw_page.get_by_role("button", name="New version", exact=True).click()
        pw_page.fill("#config-json", json.dumps(config_a))
        pw_page.fill("#config-description", "version A")
        pw_page.get_by_role("button", name="Save", exact=True).click()
        expect(pw_page.get_by_text("Config version created")).to_be_visible()
        expect(pw_page.locator("tr", has_text="v1")).to_be_visible()

        # --- View version 1 and confirm its config renders ---
        pw_page.locator("tr", has_text="v1").get_by_role("button", name="View").click()
        expect(pw_page.get_by_text("Config v1", exact=True)).to_be_visible()
        expect(pw_page.get_by_text(f'"hostname": "{config_a["hostname"]}"')).to_be_visible()
        pw_page.get_by_role("button", name="Close", exact=True).click()

        # --- Create version 2 ---
        pw_page.get_by_role("button", name="New version", exact=True).click()
        pw_page.fill("#config-json", json.dumps(config_b))
        pw_page.fill("#config-description", "version B")
        pw_page.get_by_role("button", name="Save", exact=True).click()
        expect(pw_page.get_by_text("Config version created")).to_be_visible()
        expect(pw_page.locator("tr", has_text="v2")).to_be_visible()

        # --- Diff v1 and v2: check both compare boxes, open the diff ---
        pw_page.get_by_role("checkbox", name="Compare v1").check()
        pw_page.get_by_role("checkbox", name="Compare v2").check()
        pw_page.get_by_role("button", name="Compare", exact=True).click()
        expect(pw_page.get_by_text("Config diff", exact=True)).to_be_visible()
        # The unified diff must mention the changed hostname values.
        expect(pw_page.locator("pre")).to_contain_text(config_b["hostname"])
        pw_page.get_by_role("button", name="Close", exact=True).click()

        # --- Restore version 1: writes a NEW version carrying config_a ---
        pw_page.locator("tr", has_text="v1").get_by_role("button", name="Restore").click()
        expect(pw_page.get_by_text("Restore this version?")).to_be_visible()
        # Scope to the confirm dialog: the version rows also carry Restore buttons.
        pw_page.locator("dialog[open]").get_by_role("button", name="Restore", exact=True).click()
        expect(pw_page.get_by_text("Restored as a new version")).to_be_visible()

        # --- Effect assertion via the inventory API: three versions, and the
        # newest (v3) is a restore of v1 carrying config_a verbatim ---
        listing = _poll(
            lambda: _api(pw_page, "GET", f"/inventory/devices/{device_id}/config-versions").json(),
            lambda r: r.get("total") == 3,
            timeout=15.0,
        )
        assert listing is not None, "config-version list never reached three versions"
        by_number = {v["version_number"]: v for v in listing["items"]}
        assert set(by_number) == {1, 2, 3}
        v1_id = by_number[1]["id"]
        assert by_number[3]["restored_from_id"] == v1_id, "v3 must record its restore source"

        detail_v3 = _api(
            pw_page,
            "GET",
            f"/inventory/devices/{device_id}/config-versions/{by_number[3]['id']}",
        ).json()
        assert detail_v3["config"] == config_a, "restored version must carry v1's config verbatim"
    finally:
        if device:
            _api(pw_page, "DELETE", f"/inventory/devices/{device['id']}", allow_errors=True)
        if seed:
            tid = seed["template_id"]
            _api(pw_page, "DELETE", f"/inventory/templates/{tid}", allow_errors=True)
            _api(pw_page, "DELETE", f"/inventory/drivers/{seed['driver_id']}", allow_errors=True)


# ---------------------------------------------------------------------------
# item 9: reservation create via the modal, dynamic-instance row
# ---------------------------------------------------------------------------


def _seed_dynamic_stack(page, suffix: str) -> dict:
    """Seed secret + hypervisor recipe driver + hypervisor + dynamic template.

    Mirrors the self-seeding in tests/integration/test_dynamic_resources.py so
    the CreateReservationModal's dynamic-template dropdown has a bookable option
    whose instances the execution service can actually create via the mock
    hypervisor recipe (activating the reservation).
    """
    secret = _api(
        page,
        "POST",
        "/secrets/secrets",
        json={
            "name": f"pw-hv-secret-{suffix}",
            "type": "password",
            "description": "playwright dynamic-reservation hypervisor credential",
            "data": {"username": "svc", "password": "pw-hv-password"},
        },
    ).json()

    files = {"file": (f"mock_hv-{suffix}.tar.gz", _mock_hv_tarball(), "application/gzip")}
    data = {
        "name": f"pw-mock-hv-{suffix}",
        "connection_type": "Hypervisor",
        "description": "playwright mock hypervisor recipe driver",
    }
    driver = _api(page, "POST", "/inventory/drivers", files=files, data=data).json()

    hypervisor = _api(
        page,
        "POST",
        "/inventory/hypervisors",
        json={
            "name": f"pw-hv-{suffix}",
            "description": "playwright dynamic-reservation mock hypervisor",
            "endpoint": "https://mock-hv.example:8006",
            "hypervisor_type": "mock",
            "secret_id": secret["id"],
        },
    ).json()

    template_name = f"pw-dyn-tmpl-{suffix}"
    template = _api(
        page,
        "POST",
        "/inventory/templates",
        json={
            "name": template_name,
            "template_type": "dynamic",
            "driver_id": driver["id"],
            "hypervisor_id": hypervisor["id"],
            "vendor": "PlaywrightVendor",
            "model": "MockInstance",
            "sections": [
                {
                    "name": "Instance",
                    "fields": [
                        {
                            "key": "image",
                            "label": "Image",
                            "type": "string",
                            "default": "ubuntu-22.04",
                        }
                    ],
                }
            ],
        },
    ).json()
    return {
        "secret_id": secret["id"],
        "driver_id": driver["id"],
        "hypervisor_id": hypervisor["id"],
        "template_id": template["id"],
        "template_name": template_name,
    }


def test_reservation_create_dynamic_via_modal(pw_page):
    pw_login(pw_page)
    suffix = uuid.uuid4().hex[:8]
    purpose = f"pw-dyn-res-{suffix}"
    seed = None
    reservation_id = None
    try:
        seed = _seed_dynamic_stack(pw_page, suffix)

        pw_page.goto(f"{HOST_BASE_URL}/reservations")
        pw_page.get_by_role("button", name="New Reservation", exact=True).click()
        expect(pw_page.get_by_text("Create Reservation", exact=True)).to_be_visible()

        # Add a dynamic-instance row and pick the seeded dynamic template.
        pw_page.get_by_role("button", name="Add dynamic instance", exact=True).click()
        pw_page.get_by_label("Dynamic template 1").select_option(label=seed["template_name"])

        start_local = datetime.now().replace(second=0, microsecond=0)
        end_local = start_local + timedelta(hours=1)
        pw_page.fill("#res-start", start_local.strftime("%Y-%m-%dT%H:%M"))
        pw_page.fill("#res-end", end_local.strftime("%Y-%m-%dT%H:%M"))
        pw_page.fill("#res-purpose", purpose)

        pw_page.get_by_role("button", name="Create", exact=True).click()
        expect(pw_page.get_by_text("Reservation created")).to_be_visible()

        # --- Effect assertion: the reservation exists and activates via API.
        # A dynamic reservation stays PENDING_PROVISION until the execution
        # service runs the mock hypervisor recipe and posts the activation
        # callback; poll to ACTIVE.
        created = _poll(
            lambda: next(
                (
                    r
                    for r in _api(pw_page, "GET", "/reservations/", params={"limit": 50}).json()[
                        "items"
                    ]
                    if r.get("purpose") == purpose
                ),
                None,
            ),
            lambda r: r is not None,
            timeout=15.0,
        )
        assert created is not None, "created reservation never surfaced via the API"
        reservation_id = created["id"]

        active = _poll(
            lambda: _api(pw_page, "GET", f"/reservations/{reservation_id}").json(),
            lambda r: r["status"] == "ACTIVE",
            timeout=60.0,
        )
        assert active is not None, (
            "dynamic reservation never activated: "
            f"{_api(pw_page, 'GET', f'/reservations/{reservation_id}').json()}"
        )

        # --- Cancel through the detail modal, then assert terminal via API ---
        pw_page.reload()
        row = pw_page.locator("tr", has_text=purpose)
        expect(row).to_be_visible()
        row.click()
        # Scope to the open detail dialog: the (closed) create modal shares the
        # #modal-title id, so an unscoped locator resolves to two elements.
        detail = pw_page.locator("dialog[open]")
        expect(detail.locator("#modal-title")).to_have_text("Reservation")
        detail.get_by_role("button", name="Cancel", exact=True).click()
        pw_page.get_by_role("button", name="Cancel reservation", exact=True).click()

        cancelled = _poll(
            lambda: _api(pw_page, "GET", f"/reservations/{reservation_id}").json(),
            lambda r: r["status"] in ("CANCELLED", "COMPLETED", "FAILED"),
            timeout=30.0,
        )
        assert cancelled is not None and cancelled["status"] == "CANCELLED", (
            f"reservation did not reach CANCELLED: {cancelled}"
        )
        reservation_id = None
    finally:
        if reservation_id:
            _api(pw_page, "DELETE", f"/reservations/{reservation_id}", allow_errors=True)
            _poll(
                lambda: _api(
                    pw_page, "GET", f"/reservations/{reservation_id}", allow_errors=True
                ).json(),
                lambda r: r.get("status") in ("CANCELLED", "COMPLETED", "FAILED"),
                timeout=20.0,
            )
        if seed:
            tid = seed["template_id"]
            _api(pw_page, "DELETE", f"/inventory/templates/{tid}", allow_errors=True)
            _api(
                pw_page,
                "DELETE",
                f"/inventory/hypervisors/{seed['hypervisor_id']}",
                allow_errors=True,
            )
            _api(pw_page, "DELETE", f"/inventory/drivers/{seed['driver_id']}", allow_errors=True)
            _api(pw_page, "DELETE", f"/secrets/secrets/{seed['secret_id']}", allow_errors=True)


# ---------------------------------------------------------------------------
# item 10: notification round-trip
# ---------------------------------------------------------------------------


def _my_notifications(page, reservation_id: str) -> list[tuple[int, dict]]:
    """The current admin's notifications (limit 20) whose data cites reservation_id.

    Uses the same query the NotificationBell panel issues (limit 20, offset 0),
    so an index into this list matches the panel's rendered order. Returns
    (index_in_full_list, notification) pairs.
    """
    items = _api(page, "GET", "/notifications/notifications", params={"limit": 20}).json()["items"]
    return [
        (idx, n)
        for idx, n in enumerate(items)
        if str(n.get("data", {}).get("reservation_id")) == str(reservation_id)
    ]


def test_notification_round_trip(pw_page):
    pw_login(pw_page)
    suffix = uuid.uuid4().hex[:8]
    seed = None
    device = None
    reservation_id = None
    try:
        seed = _seed_device_template(pw_page, suffix)
        device = _api(
            pw_page,
            "POST",
            "/inventory/devices",
            json={
                "name": f"pw-notif-dev-{suffix}",
                "template_id": seed["template_id"],
                "topology_type": "PHYSICAL",
                "status": "AVAILABLE",
                "field_data": {"model": "notif"},
            },
        ).json()

        # Create a physical reservation (fires reservation.created on activation),
        # then cancel it (fires reservation.cancelled): two owner notifications.
        now = datetime.now(timezone.utc)
        reservation = _api(
            pw_page,
            "POST",
            "/reservations/",
            json={
                "device_ids": [device["id"]],
                "purpose": f"pw-notif-{suffix}",
                "start_time": now.isoformat(),
                "end_time": (now + timedelta(hours=1)).isoformat(),
            },
        ).json()
        reservation_id = reservation["id"]

        active = _poll(
            lambda: _api(pw_page, "GET", f"/reservations/{reservation_id}").json(),
            lambda r: r["status"] == "ACTIVE",
            timeout=30.0,
        )
        assert active is not None, "reservation never activated to fire reservation.created"
        _api(pw_page, "DELETE", f"/reservations/{reservation_id}", allow_errors=True)

        # Wait for at least one of this test's notifications to land (async NATS
        # fan-out through the notifications consumer).
        mine = _poll(
            lambda: _my_notifications(pw_page, reservation_id),
            lambda ns: any(n["read_at"] is None for _, n in ns),
            timeout=30.0,
        )
        assert mine, "no unread notification for this reservation arrived"

        # --- UI: reload so the bell fetches immediately (unread poll is 30s),
        # then assert the badge shows an unread count (increment observed).
        pw_page.reload()
        bell = pw_page.get_by_role("button", name="Notifications")
        expect(bell).to_be_visible()
        badge = bell.locator("span.bg-red-500")
        expect(badge).to_be_visible()
        assert int(badge.inner_text().replace("+", "")) >= 1, "bell badge must show an unread count"

        # --- Mark ONE of this test's notifications read via the panel. Match by
        # index (the panel renders the same limit-20 order the API returns) so we
        # touch only our own notification, never a concurrent agent's.
        bell.click()
        expect(pw_page.get_by_text("Notifications", exact=True)).to_be_visible()

        current = _my_notifications(pw_page, reservation_id)
        target_idx, target = next((i, n) for i, n in current if n["read_at"] is None)

        item_buttons = pw_page.locator("button").filter(
            has=pw_page.get_by_role("button", name="Delete notification")
        )
        # Click the title area (top-left) to avoid the row's Delete "x".
        item_buttons.nth(target_idx).click(position={"x": 16, "y": 16})

        # --- Effect assertion via the notifications API: that exact
        # notification flipped to read (its contribution to the unread count
        # dropped to zero).
        read_now = _poll(
            lambda: next(
                (
                    n
                    for _, n in _my_notifications(pw_page, reservation_id)
                    if n["id"] == target["id"]
                ),
                None,
            ),
            lambda n: n is not None and n["read_at"] is not None,
            timeout=15.0,
        )
        assert read_now is not None, "the marked notification never flipped to read via the API"
    finally:
        if reservation_id:
            _api(pw_page, "DELETE", f"/reservations/{reservation_id}", allow_errors=True)
            _poll(
                lambda: _api(
                    pw_page, "GET", f"/reservations/{reservation_id}", allow_errors=True
                ).json(),
                lambda r: r.get("status") in ("CANCELLED", "COMPLETED", "FAILED"),
                timeout=20.0,
            )
        if device:
            _api(pw_page, "DELETE", f"/inventory/devices/{device['id']}", allow_errors=True)
        if seed:
            tid = seed["template_id"]
            _api(pw_page, "DELETE", f"/inventory/templates/{tid}", allow_errors=True)
            _api(pw_page, "DELETE", f"/inventory/drivers/{seed['driver_id']}", allow_errors=True)
