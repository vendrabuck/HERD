"""Playwright e2e test for the layered Wiring tab: L2 membership rows, retry,
and a re-wire that releases membership (ADR 0009 phase 8, issue #416).

The e2e half of the ADR 0009 test plan: the Wiring tab shows layered rows, an
L2 re-wire surfaces membership status, and retry follows the effect-assertion
discipline established in test_config_playwright.py and mirrored from
test_wiring_failed_row_playwright.py (the L1 analogue of this test). The
API-level L2 scenario is already proven live in
tests/integration/test_l2_reconcile.py; this test drives the FAILED membership
observation, the retry click, and the layered rendering through the actual
ReservationWiringTab UI, confirming every UI action by an API read-back.

Self-seeds a mock L2 switch driver/template (drivers/mock_l2) armed with
HERD_mock_fail_actions=add_to_vlan, a bare Management-connection DUT
driver/template, two DUT devices cabled through the switch, a topology, and a
reservation. The reservation activates against an EMPTY canvas so the fail
knob is inert at activation; saving the wired canvas is what drives the L2
membership reconcile (each DUT's switch-side port joins the fabric VLAN), and
the armed knob fails both joins, landing FAILED l2_port_assignments rows that
surface as layer "l2" wiring-status rows.

The default auto-retry channel (WIRING_RETRY_INTERVAL_SECONDS=60,
WIRING_RETRY_MAX_ATTEMPTS=10, docs/ENV_VARS.md) is not overridden in the dev
stack. The background sweep CAN fire mid-test (its 60s tick is wall-clock from
service start, not from row creation), but every interleaving converges: a
sweep while the fail knob is armed only increments attempts (asserted below
the cap before the manual retry click), and a sweep after the knob clears
flips the rows ACTIVE server-side, in which case the manual retry is a no-op
that still passes the ACTIVE read-back. Do not weaken the attempts-cap guard
on the strength of the happy-path timing.
"""

import io
import tarfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from playwright.sync_api import expect

from .conftest import HOST_BASE_URL, pw_login

_MOCK_L2_DIR = Path(__file__).resolve().parents[2] / "drivers" / "mock_l2"

# services/execution/app/config.py default (docs/ENV_VARS.md); not overridden
# in docker-compose.override.yml, so the auto-retry channel ticks every 60s.
_WIRING_RETRY_MAX_ATTEMPTS_DEFAULT = 10

# nats_consumer.WIRING_DRIVER_ATTEMPTS: the apply path's in-line retry cap. A
# FAILED membership row is only stable once the apply has exhausted these
# (attempts reaches this value AND last_applied_fork_version reaches the saved
# version); retrying before then races the still-in-flight apply, whose
# completion clobbers a successful retry back to FAILED (issue #412).
_WIRING_DRIVER_ATTEMPTS = 3


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


def _mock_l2_tarball() -> bytes:
    """Package the checked-in drivers/mock_l2 package into a .tar.gz for upload."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name in ("driver.py", "driver_metadata.json"):
            tf.add(_MOCK_L2_DIR / name, arcname=name)
    return buf.getvalue()


def _dut_driver_tarball() -> bytes:
    """A minimal no-op Management driver, just enough for a DUT template."""
    body = b"class Driver:\n    pass\n"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo("driver.py")
        info.size = len(body)
        tf.addfile(info, io.BytesIO(body))
    return buf.getvalue()


def _canvas_edge(a_id: str, b_id: str) -> dict:
    return {
        "nodes": [
            {"id": "nA", "data": {"device": {"id": a_id}}},
            {"id": "nB", "data": {"device": {"id": b_id}}},
        ],
        "edges": [
            {
                "id": "e1",
                "source": "nA",
                "target": "nB",
                "data": {"layer": "L1", "isProposal": False},
            }
        ],
    }


def _poll(fn, predicate, *, timeout: float = 20.0, interval: float = 0.5):
    """Call fn() until predicate(result) is true or timeout elapses.

    Returns the predicate-satisfying result, or None on timeout, so an
    assertion on the return value can never accidentally pass against a
    last-known-bad snapshot (the issue #412 lesson from the L1 analogue).
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = fn()
        if predicate(result):
            return result
        time.sleep(interval)
    return None


def _l2_rows(wiring: dict) -> list[dict]:
    return [c for c in wiring.get("connections", []) if c.get("layer") == "l2"]


def test_wiring_tab_layered_l2_membership_retry_and_release(pw_page):
    pw_login(pw_page)
    suffix = uuid.uuid4().hex[:8]

    driver_id = None
    dut_driver_id = None
    template_id = None
    dut_template_id = None
    switch = None
    dut_a = None
    dut_b = None
    connections: list[dict] = []
    topology_id = None
    reservation_id = None

    try:
        # --- Seed: mock L2 switch driver + template, armed to fail add_to_vlan ---
        files = {"file": ("mock_l2.tar.gz", _mock_l2_tarball(), "application/gzip")}
        data = {
            "name": f"pw-mock-l2-{suffix}",
            "connection_type": "Layer 2 Switch",
            "description": "playwright layered wiring-tab test L2 switch driver",
        }
        driver = _api(pw_page, "POST", "/inventory/drivers", files=files, data=data).json()
        driver_id = driver["id"]

        template = _api(
            pw_page,
            "POST",
            "/inventory/templates",
            json={
                "name": f"pw-mock-l2-tmpl-{suffix}",
                "template_type": "device",
                "driver_id": driver_id,
                "vendor": "PlaywrightVendor",
                "model": "MockL2Switch",
                "sections": [
                    {
                        "name": "General",
                        "fields": [
                            {"key": "model", "label": "Model", "type": "string"},
                            {
                                "key": "mock_fail_actions",
                                "label": "Mock fail actions",
                                "type": "string",
                            },
                        ],
                    }
                ],
            },
        ).json()
        template_id = template["id"]

        # --- Seed: bare Management driver + template for the two DUTs ---
        dut_files = {"file": ("dut-driver.tar.gz", _dut_driver_tarball(), "application/gzip")}
        dut_data = {
            "name": f"pw-dut-driver-{suffix}",
            "connection_type": "Management",
            "description": "playwright layered wiring-tab test DUT driver",
        }
        dut_driver = _api(
            pw_page, "POST", "/inventory/drivers", files=dut_files, data=dut_data
        ).json()
        dut_driver_id = dut_driver["id"]

        dut_template = _api(
            pw_page,
            "POST",
            "/inventory/templates",
            json={
                "name": f"pw-dut-tmpl-{suffix}",
                "template_type": "device",
                "driver_id": dut_driver_id,
                "vendor": "PlaywrightVendor",
                "model": "DUT",
                "sections": [
                    {
                        "name": "General",
                        "fields": [{"key": "model", "label": "Model", "type": "string"}],
                    }
                ],
            },
        ).json()
        dut_template_id = dut_template["id"]

        # --- Switch device with the fail knob armed on add_to_vlan ---
        switch = _api(
            pw_page,
            "POST",
            "/inventory/devices",
            json={
                "name": f"pw-l2-sw-{suffix}",
                "template_id": template_id,
                "topology_type": "PHYSICAL",
                "status": "AVAILABLE",
                "field_data": {"model": "test", "mock_fail_actions": "add_to_vlan"},
            },
        ).json()

        # --- Two DUT devices ---
        dut_a = _api(
            pw_page,
            "POST",
            "/inventory/devices",
            json={
                "name": f"pw-dut-a-{suffix}",
                "template_id": dut_template_id,
                "topology_type": "PHYSICAL",
                "status": "AVAILABLE",
                "field_data": {"model": "test"},
            },
        ).json()
        dut_b = _api(
            pw_page,
            "POST",
            "/inventory/devices",
            json={
                "name": f"pw-dut-b-{suffix}",
                "template_id": dut_template_id,
                "topology_type": "PHYSICAL",
                "status": "AVAILABLE",
                "field_data": {"model": "test"},
            },
        ).json()

        # --- Cable each DUT to a switch port (matches test_l2_reconcile.py) ---
        connections.append(
            _api(
                pw_page,
                "POST",
                "/cabling/connections",
                json={
                    "device_a_id": dut_a["id"],
                    "port_a": "eth0",
                    "device_b_id": switch["id"],
                    "port_b": "1",
                    "connection_type": "L1",
                },
            ).json()
        )
        connections.append(
            _api(
                pw_page,
                "POST",
                "/cabling/connections",
                json={
                    "device_a_id": dut_b["id"],
                    "port_a": "eth0",
                    "device_b_id": switch["id"],
                    "port_b": "2",
                    "connection_type": "L1",
                },
            ).json()
        )

        # --- Topology: activate against an EMPTY canvas so the fail knob is
        # inert at activation; the fork-save reconcile below is what drives the
        # failing add_to_vlan joins.
        topo = _api(
            pw_page, "POST", "/cabling/topologies", json={"name": f"pw-l2-wiring-topo-{suffix}"}
        ).json()
        topology_id = topo["id"]
        _api(
            pw_page,
            "PUT",
            f"/cabling/topologies/{topology_id}",
            json={"canvas_data": {"nodes": [], "edges": []}},
        )

        # --- Reservation, starting now (switch included, as in the L2
        # integration test, so its membership work is in-reservation) ---
        now = datetime.now(timezone.utc)
        purpose = f"pw-l2-wiring-e2e-{suffix}"
        reservation = _api(
            pw_page,
            "POST",
            "/reservations/",
            json={
                "device_ids": [dut_a["id"], dut_b["id"], switch["id"]],
                "topology_id": topology_id,
                "purpose": purpose,
                "start_time": now.isoformat(),
                "end_time": (now + timedelta(hours=1)).isoformat(),
            },
        ).json()
        reservation_id = reservation["id"]

        active = _poll(
            lambda: _api(pw_page, "GET", f"/reservations/{reservation_id}").json(),
            lambda r: r["status"] == "ACTIVE",
            timeout=20.0,
        )
        assert active is not None and active["status"] == "ACTIVE", (
            f"reservation never activated: {active}"
        )

        # --- Save the wired canvas: the L2 reconcile joins ports 1 and 2 to
        # the fabric VLAN, add_to_vlan fails, and FAILED l2_port_assignments
        # rows land, surfaced as layer "l2" wiring-status rows.
        saved = _api(
            pw_page,
            "POST",
            f"/reservations/{reservation_id}/fork/save",
            json={"canvas_data": _canvas_edge(dut_a["id"], dut_b["id"])},
        )
        assert saved.status_code == 200, saved.text
        saved_version = saved.json()["version_number"]

        # Wait for the apply to COMPLETE, not merely for a FAILED row to appear
        # (the issue #412 discipline from the L1 analogue).
        wiring = _poll(
            lambda: _api(pw_page, "GET", f"/reservations/{reservation_id}/wiring-status").json(),
            lambda w: (
                w.get("last_applied_fork_version") == saved_version
                and len(_l2_rows(w)) == 2
                and all(
                    c["status"] == "FAILED" and c["attempts"] >= _WIRING_DRIVER_ATTEMPTS
                    for c in _l2_rows(w)
                )
            ),
            timeout=25.0,
        )
        assert wiring is not None, "the L2 apply never completed with two stable FAILED rows"
        for row in _l2_rows(wiring):
            assert row["retryable"] is True, "a driver-failure membership row must be retryable"
            assert row["intended"] == "ACTIVE"
            assert "add_to_vlan" in (row["last_error"] or "")
            assert isinstance(row["vlan"], int), "allocation precedes the driver call"
            assert row["port"] in ("1", "2")
            assert row["attempts"] < _WIRING_RETRY_MAX_ATTEMPTS_DEFAULT, (
                "attempts is already near the auto-retry cap; the failed window ran too long"
            )

        # --- UI: open the reservation from the reservations page, switch to
        # Wiring, and assert the LAYERED rendering: the L2 section heading, the
        # membership detail line, and the FAILED state.
        pw_page.goto(f"{HOST_BASE_URL}/reservations")
        row = pw_page.locator("tr", has_text=purpose)
        expect(row).to_be_visible()
        row.click()

        dialog = pw_page.locator("dialog[open]")
        expect(dialog.locator("#modal-title")).to_have_text("Reservation")
        dialog.get_by_role("button", name="Wiring", exact=True).click()

        expect(dialog.get_by_text("L2 VLAN memberships", exact=True)).to_be_visible()
        vlan_number = _l2_rows(wiring)[0]["vlan"]
        expect(dialog.get_by_text(f"port 1 VLAN {vlan_number}", exact=True)).to_be_visible()
        expect(dialog.get_by_text(f"port 2 VLAN {vlan_number}", exact=True)).to_be_visible()
        expect(dialog.get_by_text("FAILED", exact=True).first).to_be_visible()
        expect(dialog.get_by_text("mock injected failure on add_to_vlan").first).to_be_visible()
        retry_button = dialog.get_by_role("button", name="Retry failed", exact=True)
        expect(retry_button).to_be_visible()

        # --- Clear the fail knob so the retried joins can succeed ---
        cleared = _api(
            pw_page,
            "PUT",
            f"/inventory/devices/{switch['id']}",
            json={"field_data": {"model": "test", "mock_fail_actions": ""}},
        )
        assert cleared.status_code == 200, cleared.text

        # --- UI: click Retry failed; assert the retry-summary toast, then the
        # EFFECT (both membership rows flip ACTIVE) via API read-back.
        retry_button.click()
        expect(pw_page.get_by_text("Retry complete:")).to_be_visible(timeout=10000)

        active_wiring = _poll(
            lambda: _api(pw_page, "GET", f"/reservations/{reservation_id}/wiring-status").json(),
            lambda w: len(_l2_rows(w)) == 2 and all(c["status"] == "ACTIVE" for c in _l2_rows(w)),
            timeout=15.0,
        )
        assert active_wiring is not None, (
            "the retried memberships never reached ACTIVE via read-back"
        )

        # The panel refreshes on retry success; the FAILED badges are gone.
        expect(dialog.get_by_text("ACTIVE", exact=True).first).to_be_visible(timeout=10000)
        expect(dialog.get_by_text("FAILED", exact=True)).to_have_count(0)
        dialog.get_by_role("button", name="Close dialog", exact=True).click()

        # --- L2 re-wire: save an emptied canvas. The full membership reconcile
        # releases both ports (this is also the hardware restore-to-baseline).
        emptied = _api(
            pw_page,
            "POST",
            f"/reservations/{reservation_id}/fork/save",
            json={"canvas_data": {"nodes": [], "edges": []}},
        )
        assert emptied.status_code == 200, emptied.text
        emptied_version = emptied.json()["version_number"]

        released_wiring = _poll(
            lambda: _api(pw_page, "GET", f"/reservations/{reservation_id}/wiring-status").json(),
            lambda w: (
                w.get("last_applied_fork_version") == emptied_version
                and len(_l2_rows(w)) == 2
                and all(c["status"] == "RELEASED" for c in _l2_rows(w))
            ),
            timeout=25.0,
        )
        assert released_wiring is not None, (
            "the emptied-canvas save never released both memberships via read-back"
        )

        # --- UI: reopen the tab and assert the re-wire surfaced as RELEASED
        # membership rows (the panel refetches on mount).
        pw_page.goto(f"{HOST_BASE_URL}/reservations")
        row = pw_page.locator("tr", has_text=purpose)
        expect(row).to_be_visible()
        row.click()
        dialog = pw_page.locator("dialog[open]")
        expect(dialog.locator("#modal-title")).to_have_text("Reservation")
        dialog.get_by_role("button", name="Wiring", exact=True).click()
        expect(dialog.get_by_text("L2 VLAN memberships", exact=True)).to_be_visible()
        expect(dialog.get_by_text("RELEASED", exact=True).first).to_be_visible()
        expect(dialog.get_by_text("FAILED", exact=True)).to_have_count(0)
    finally:
        if reservation_id:
            _api(pw_page, "DELETE", f"/reservations/{reservation_id}", allow_errors=True)
            _poll(
                lambda: _api(
                    pw_page, "GET", f"/reservations/{reservation_id}", allow_errors=True
                ).json(),
                lambda r: r.get("status") in ("CANCELLED", "COMPLETED", "FAILED"),
                timeout=15.0,
            )
        if topology_id:
            _api(pw_page, "DELETE", f"/cabling/topologies/{topology_id}", allow_errors=True)
        for conn in connections:
            _api(pw_page, "DELETE", f"/cabling/connections/{conn['id']}", allow_errors=True)
        if switch:
            _api(pw_page, "DELETE", f"/inventory/devices/{switch['id']}", allow_errors=True)
        if dut_a:
            _api(pw_page, "DELETE", f"/inventory/devices/{dut_a['id']}", allow_errors=True)
        if dut_b:
            _api(pw_page, "DELETE", f"/inventory/devices/{dut_b['id']}", allow_errors=True)
        if template_id:
            _api(pw_page, "DELETE", f"/inventory/templates/{template_id}", allow_errors=True)
        if dut_template_id:
            _api(pw_page, "DELETE", f"/inventory/templates/{dut_template_id}", allow_errors=True)
        if driver_id:
            _api(pw_page, "DELETE", f"/inventory/drivers/{driver_id}", allow_errors=True)
        if dut_driver_id:
            _api(pw_page, "DELETE", f"/inventory/drivers/{dut_driver_id}", allow_errors=True)
