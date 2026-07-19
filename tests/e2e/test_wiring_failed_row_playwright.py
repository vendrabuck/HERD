"""Playwright e2e test for the Wiring tab's FAILED row and Retry-failed flow,
driven by real provisioning (issue #388 item 12).

Mirrors the API-level scenario already proven live in
tests/integration/test_wiring_changed_reconcile.py
(test_failed_build_surfaces_and_manual_retry_recovers), but drives the FAILED
row observation and the retry click through the actual
ReservationWiringTab UI (frontend/src/components/reservations/
ReservationWiringTab.tsx) instead of calling the API directly for those two
steps. Every UI action is still confirmed by an API read-back, per the
effect-assertion discipline established in test_config_playwright.py.

Self-seeds a mock L1 switch driver/template (drivers/mock_l1), a bare
Management-connection DUT driver/template, two DUT devices, an L1 switch
armed with HERD_mock_fail_actions=connect_ports, a topology, and a
reservation. The reservation activates against an EMPTY canvas so the fail
knob is inert at activation (matches the integration test's technique);
saving the wired canvas afterward is what drives the reconcile that builds
the switch cross-connect and fails it, landing a FAILED
l1_connection_assignments row.

The default auto-retry channel (WIRING_RETRY_INTERVAL_SECONDS=60,
WIRING_RETRY_MAX_ATTEMPTS=10, docs/ENV_VARS.md) is not overridden in the dev
stack, so a test that finishes well inside that 60s window cannot race the
background sweep into parking the row past the attempts cap. The attempts
count is asserted below the cap before the manual retry click as a guard
against a slow run.
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

_MOCK_L1_DIR = Path(__file__).resolve().parents[2] / "drivers" / "mock_l1"

# services/execution/app/config.py default (docs/ENV_VARS.md); not overridden
# in docker-compose.override.yml, so the auto-retry channel ticks every 60s.
_WIRING_RETRY_MAX_ATTEMPTS_DEFAULT = 10

# nats_consumer.WIRING_DRIVER_ATTEMPTS: the apply path's in-line retry cap.
# The FAILED row is only stable once the apply has exhausted these (attempts
# reaches this value AND last_applied_fork_version reaches the saved version);
# retrying before then races the still-in-flight apply, whose completion
# clobbers a successful retry back to FAILED (issue #412).
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


def _mock_l1_tarball() -> bytes:
    """Package the checked-in drivers/mock_l1 package into a .tar.gz for upload."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name in ("driver.py", "driver_metadata.json"):
            tf.add(_MOCK_L1_DIR / name, arcname=name)
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
    last-known-bad snapshot (that ambiguity hid the issue #412 race when
    this test was first written).
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = fn()
        if predicate(result):
            return result
        time.sleep(interval)
    return None


def test_wiring_tab_failed_row_and_retry(pw_page):
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
        # --- Seed: mock L1 switch driver + template, armed to fail connect_ports ---
        files = {"file": ("mock_l1.tar.gz", _mock_l1_tarball(), "application/gzip")}
        data = {
            "name": f"pw-mock-l1-{suffix}",
            "connection_type": "Layer 1 Switch",
            "description": "playwright wiring-tab test L1 switch driver",
        }
        driver = _api(pw_page, "POST", "/inventory/drivers", files=files, data=data).json()
        driver_id = driver["id"]

        template = _api(
            pw_page,
            "POST",
            "/inventory/templates",
            json={
                "name": f"pw-mock-l1-tmpl-{suffix}",
                "template_type": "device",
                "driver_id": driver_id,
                "vendor": "PlaywrightVendor",
                "model": "MockL1Switch",
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
            "description": "playwright wiring-tab test DUT driver",
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

        # --- Switch device with the fail knob armed on connect_ports ---
        switch = _api(
            pw_page,
            "POST",
            "/inventory/devices",
            json={
                "name": f"pw-l1-sw-{suffix}",
                "template_id": template_id,
                "topology_type": "PHYSICAL",
                "status": "AVAILABLE",
                "field_data": {"model": "test", "mock_fail_actions": "connect_ports"},
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

        # --- Cable each DUT to a switch port ---
        connections.append(
            _api(
                pw_page,
                "POST",
                "/cabling/connections",
                json={
                    "device_a_id": dut_a["id"],
                    "port_a": "eth0",
                    "device_b_id": switch["id"],
                    "port_b": "p1",
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
                    "port_b": "p2",
                    "connection_type": "L1",
                },
            ).json()
        )

        # --- Topology: activate against an EMPTY canvas so the fail knob is
        # inert at activation (mirrors test_wiring_changed_reconcile.py); the
        # fork-save reconcile below is what drives the failing connect_ports.
        topo = _api(
            pw_page, "POST", "/cabling/topologies", json={"name": f"pw-wiring-topo-{suffix}"}
        ).json()
        topology_id = topo["id"]
        _api(
            pw_page,
            "PUT",
            f"/cabling/topologies/{topology_id}",
            json={"canvas_data": {"nodes": [], "edges": []}},
        )

        # --- Reservation, starting now ---
        now = datetime.now(timezone.utc)
        purpose = f"pw-wiring-e2e-{suffix}"
        reservation = _api(
            pw_page,
            "POST",
            "/reservations/",
            json={
                "device_ids": [dut_a["id"], dut_b["id"]],
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

        # --- Save the wired canvas: the reconcile builds the switch pair,
        # connect_ports fails, and a FAILED l1_connection_assignments row lands.
        saved = _api(
            pw_page,
            "POST",
            f"/reservations/{reservation_id}/fork/save",
            json={"canvas_data": _canvas_edge(dut_a["id"], dut_b["id"])},
        )
        assert saved.status_code == 200, saved.text
        saved_version = saved.json()["version_number"]

        # Wait for the apply to COMPLETE, not merely for a FAILED row to appear:
        # the row is recorded progressively while the apply's in-line retries
        # back off, and acting on it mid-apply reproduces the issue #412 race.
        wiring = _poll(
            lambda: _api(pw_page, "GET", f"/reservations/{reservation_id}/wiring-status").json(),
            lambda w: (
                w.get("last_applied_fork_version") == saved_version
                and any(
                    c["status"] == "FAILED" and c["attempts"] >= _WIRING_DRIVER_ATTEMPTS
                    for c in w.get("connections", [])
                )
            ),
            timeout=25.0,
        )
        assert wiring is not None, "the wiring apply never completed with a stable FAILED row"
        failed_conn = next(c for c in wiring["connections"] if c["status"] == "FAILED")
        assert failed_conn["retryable"] is True, "a driver-failure row must be retryable"
        assert "connect_ports" in (failed_conn["last_error"] or "")
        assert failed_conn["attempts"] < _WIRING_RETRY_MAX_ATTEMPTS_DEFAULT, (
            "attempts is already near the auto-retry cap; the failed window ran too long"
        )

        # --- UI: open the reservation from the reservations page, switch to Wiring ---
        pw_page.goto(f"{HOST_BASE_URL}/reservations")
        row = pw_page.locator("tr", has_text=purpose)
        expect(row).to_be_visible()
        row.click()

        dialog = pw_page.locator("dialog[open]")
        expect(dialog.locator("#modal-title")).to_have_text("Reservation")
        dialog.get_by_role("button", name="Wiring", exact=True).click()

        expect(dialog.get_by_text("FAILED", exact=True)).to_be_visible()
        expect(dialog.get_by_text("mock injected failure on connect_ports")).to_be_visible()
        expect(dialog.get_by_text("Attempts:")).to_be_visible()
        retry_button = dialog.get_by_role("button", name="Retry failed", exact=True)
        expect(retry_button).to_be_visible()

        # --- Clear the fail knob so the retried driver call can succeed ---
        cleared = _api(
            pw_page,
            "PUT",
            f"/inventory/devices/{switch['id']}",
            json={"field_data": {"model": "test", "mock_fail_actions": ""}},
        )
        assert cleared.status_code == 200, cleared.text

        # --- UI: click Retry failed; assert the retry-summary toast, then the
        # EFFECT (wiring-status flips ACTIVE and the panel reflects it) ---
        retry_button.click()
        expect(pw_page.get_by_text("Retry complete:")).to_be_visible(timeout=10000)

        active_wiring = _poll(
            lambda: _api(pw_page, "GET", f"/reservations/{reservation_id}/wiring-status").json(),
            lambda w: (
                w.get("connections")
                and all(c["status"] != "FAILED" for c in w["connections"])
                and any(c["status"] == "ACTIVE" for c in w["connections"])
            ),
            timeout=15.0,
        )
        assert active_wiring is not None, (
            "the retried connection never reached ACTIVE with no FAILED rows left"
        )

        expect(dialog.get_by_text("ACTIVE", exact=True)).to_be_visible(timeout=10000)
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
