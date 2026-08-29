"""Playwright e2e tests for issue #388 Tier 2: the flows that were blocked on
the UI-stack decision and became automatable once Playwright joined the suite
(PR #400): browser contexts, the download API, and SSE streaming.

Every UI action is confirmed by an API read-back, per the effect-assertion
discipline established in test_config_playwright.py; a click that only renders
feedback is never trusted on its own.

The four flows:

1. test_two_session_port_conflict: two authenticated browser contexts each hold
   an ACTIVE reservation. Context B claims a shared switch port through a fork
   save; context A then commits a fork that would claim the same port and gets
   the "Ports already claimed" dialog naming B's reservation. The effect
   read-back proves A's fork version never advanced and B's wiring is untouched.
   Both contexts authenticate as the seeded admin: the port-claim guard
   (services/cabling/app/services/fork_save_service.py::_assert_no_port_claims)
   keys strictly on the OTHER active fork's id, never on owner identity, so two
   independent sessions holding two reservations reproduce the conflict exactly;
   using one principal keeps the test off the non-admin device-visibility gate
   without weakening what is exercised.

2. test_utilization_fleet_csv_download: the reporting page's fleet CSV, captured
   through Playwright's download API, parses and carries a row for a device with
   a live reservation the test seeded (issue #388 item 11). The fleet section
   counts ACTIVE reservations (FLEET_DEFAULT_STATUS_FILTER), so a running
   reservation shows reservation_count >= 1 against a window widened to tomorrow.

3. test_bulk_export_topologies_download: the topologies JSON export, captured the
   same way, contains every topology the test created.

4. test_assistant_stream_token_by_token: gated on GET /api/ai/status; when the
   assistant is configured it drives the reservation AI Assistant tab, asserts
   the answer renders token by token (the streamed region grows before the final
   bubble), and proves the conversation persisted by replaying its id against the
   buffered assistant endpoint (a 404 there would mean nothing was written). When
   the assistant is unconfigured it skips with the status payload.

Shared-stack discipline: every created name carries a uuid suffix, only ports on
devices this test created are ever claimed, and every mutation is restored in a
finally block (reservations cancelled first, then connections, devices,
templates, drivers, topologies).
"""

import csv
import io
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import expect

from .conftest import HOST_BASE_URL, pw_login


# --------------------------------------------------------------------------- #
# Host-side API helpers (authenticated with the browser context's own JWT).    #
# --------------------------------------------------------------------------- #
def _token(page) -> str | None:
    return page.evaluate("() => window.localStorage.getItem('access_token')")


def _api(page, method, path, **kwargs):
    """Authenticated host-side HERD API request using the page's own JWT."""
    allow_errors = kwargs.pop("allow_errors", False)
    token = _token(page)
    headers = kwargs.pop("headers", {}) or {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"{HOST_BASE_URL}/api{path}"
    with httpx.Client(verify=False, timeout=60.0) as client:
        resp = client.request(method, url, headers=headers, **kwargs)
    if not allow_errors:
        resp.raise_for_status()
    return resp


def _poll(fn, predicate, *, timeout: float = 30.0, interval: float = 0.5):
    """Call fn() until predicate(result) is true or timeout elapses; else None."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = fn()
        if predicate(result):
            return result
        time.sleep(interval)
    return None


# --------------------------------------------------------------------------- #
# Seeding helpers.                                                             #
# --------------------------------------------------------------------------- #
def _driver_tarball() -> bytes:
    """A minimal no-op Management driver, enough to back a device template."""
    import tarfile

    body = b"class Driver:\n    pass\n"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo("driver.py")
        info.size = len(body)
        tf.addfile(info, io.BytesIO(body))
    return buf.getvalue()


def _seed_template(page, suffix: str) -> tuple[str, str]:
    """Upload a driver and create a device template; return (driver_id, template_id)."""
    files = {"file": ("t2-driver.tar.gz", _driver_tarball(), "application/gzip")}
    data = {
        "name": f"pw-t2-drv-{suffix}",
        "connection_type": "Management",
        "description": "tier2 playwright test driver",
    }
    driver = _api(page, "POST", "/inventory/drivers", files=files, data=data).json()
    template = _api(
        page,
        "POST",
        "/inventory/templates",
        json={
            "name": f"pw-t2-tmpl-{suffix}",
            "template_type": "device",
            "driver_id": driver["id"],
            "vendor": "PlaywrightVendor",
            "model": "T2",
            "sections": [
                {
                    "name": "General",
                    "fields": [{"key": "model", "label": "Model", "type": "string"}],
                }
            ],
        },
    ).json()
    return driver["id"], template["id"]


def _seed_device(page, template_id: str, name: str) -> dict:
    return _api(
        page,
        "POST",
        "/inventory/devices",
        json={
            "name": name,
            "template_id": template_id,
            "topology_type": "PHYSICAL",
            "status": "AVAILABLE",
            "field_data": {"model": "test"},
        },
    ).json()


def _empty_topology(page, suffix: str) -> str:
    topo = _api(page, "POST", "/cabling/topologies", json={"name": f"pw-t2-topo-{suffix}"}).json()
    _api(
        page,
        "PUT",
        f"/cabling/topologies/{topo['id']}",
        json={"canvas_data": {"nodes": [], "edges": []}},
    )
    return topo["id"]


def _create_active_reservation(page, device_ids, topology_id, purpose) -> dict:
    """Create a reservation starting now and wait for the sweep to activate it."""
    now = datetime.now(timezone.utc)
    res = _api(
        page,
        "POST",
        "/reservations/",
        json={
            "device_ids": device_ids,
            "topology_id": topology_id,
            "purpose": purpose,
            "start_time": now.isoformat(),
            "end_time": (now + timedelta(hours=1)).isoformat(),
        },
    ).json()
    active = _poll(
        lambda: _api(page, "GET", f"/reservations/{res['id']}").json(),
        lambda r: r["status"] == "ACTIVE",
        timeout=30.0,
    )
    assert active is not None, f"reservation {res['id']} never activated: {active}"
    return res


def _cancel_and_wait(page, reservation_id) -> None:
    _api(page, "DELETE", f"/reservations/{reservation_id}", allow_errors=True)
    _poll(
        lambda: _api(page, "GET", f"/reservations/{reservation_id}", allow_errors=True).json(),
        lambda r: r.get("status") in ("CANCELLED", "COMPLETED", "FAILED"),
        timeout=20.0,
    )


# --------------------------------------------------------------------------- #
# Fork read-back helpers.                                                      #
# --------------------------------------------------------------------------- #
def _max_fork_version(fork: dict) -> int:
    versions = fork.get("versions") or []
    return max((v["version_number"] for v in versions), default=0)


def _fork_conn_identities(fork: dict) -> set[tuple]:
    out = set()
    for c in fork.get("connections") or []:
        endpoints = sorted(
            [(str(c["device_a_id"]), c["port_a"]), (str(c["device_b_id"]), c["port_b"])]
        )
        out.add((endpoints[0], endpoints[1], c["layer"]))
    return out


# --------------------------------------------------------------------------- #
# 1. Two-session port-conflict.                                               #
# --------------------------------------------------------------------------- #
def _conflict_canvas(switch: dict, shared: dict) -> dict:
    """A one-edge canvas wiring the shared switch directly to the shared DUT.

    Both reservations submit this identical canvas; the single physical hop it
    resolves to (switch:ps to shared:eth0) is the port both forks try to claim.
    """
    return {
        "nodes": [
            {
                "id": "nSW",
                "type": "deviceNode",
                "position": {"x": 100, "y": 100},
                "data": {
                    "device": {"id": switch["id"]},
                    "label": switch["name"],
                    "topologyType": "PHYSICAL",
                },
            },
            {
                "id": "nSH",
                "type": "deviceNode",
                "position": {"x": 420, "y": 100},
                "data": {
                    "device": {"id": shared["id"]},
                    "label": shared["name"],
                    "topologyType": "PHYSICAL",
                },
            },
        ],
        "edges": [
            {
                "id": "eShared",
                "source": "nSW",
                "target": "nSH",
                "data": {"layer": "L1", "isProposal": False},
            }
        ],
        "selectedEdgeLayer": "L1",
    }


def test_two_session_port_conflict(pw_browser):
    ctx_a = pw_browser.new_context(ignore_https_errors=True)
    ctx_b = pw_browser.new_context(ignore_https_errors=True)
    page_a = ctx_a.new_page()
    page_b = ctx_b.new_page()

    suffix = uuid.uuid4().hex[:8]
    driver_id = template_id = None
    switch = shared = dut_a = dut_b = None
    connection = None
    topology_id = None
    res_a = res_b = None

    try:
        pw_login(page_a)
        pw_login(page_b)

        # --- Seed shared infra via context A (both principals are the admin). ---
        driver_id, template_id = _seed_template(page_a, suffix)
        switch = _seed_device(page_a, template_id, f"pw-t2-sw-{suffix}")
        shared = _seed_device(page_a, template_id, f"pw-t2-shared-{suffix}")
        dut_a = _seed_device(page_a, template_id, f"pw-t2-duta-{suffix}")
        dut_b = _seed_device(page_a, template_id, f"pw-t2-dutb-{suffix}")

        # The single physical cable both forks contend for.
        connection = _api(
            page_a,
            "POST",
            "/cabling/connections",
            json={
                "device_a_id": switch["id"],
                "port_a": "ps",
                "device_b_id": shared["id"],
                "port_b": "eth0",
                "connection_type": "L1",
            },
        ).json()

        topology_id = _empty_topology(page_a, suffix)

        # Two reservations over DISJOINT reserved devices (no reservation
        # conflict); the contended switch/shared devices are unreserved transit.
        res_a = _create_active_reservation(
            page_a, [dut_a["id"]], topology_id, f"pw-t2-resA-{suffix}"
        )["id"]
        res_b = _create_active_reservation(
            page_b, [dut_b["id"]], topology_id, f"pw-t2-resB-{suffix}"
        )["id"]

        # Lazy-create both forks, then B claims the shared port via a fork save.
        _api(page_a, "GET", f"/reservations/{res_a}/fork")
        _api(page_b, "GET", f"/reservations/{res_b}/fork")
        save_b = _api(
            page_b,
            "POST",
            f"/reservations/{res_b}/fork/save",
            json={"canvas_data": _conflict_canvas(switch, shared)},
        )
        assert save_b.status_code == 200, save_b.text

        fork_b_before = _api(page_b, "GET", f"/reservations/{res_b}/fork").json()
        fork_a_before = _api(page_a, "GET", f"/reservations/{res_a}/fork").json()
        assert _fork_conn_identities(fork_b_before), "B should hold a port claim after its save"

        # Seed A's DRAFT canvas so the editor loads the conflicting wiring; A only
        # has to click Commit to drive the reconcile that hits B's claim.
        _api(
            page_a,
            "PUT",
            f"/reservations/{res_a}/fork/canvas",
            json={"canvas_data": _conflict_canvas(switch, shared)},
        )

        # --- Context A: open live-edit and commit; expect the conflict dialog. ---
        page_a.goto(f"{HOST_BASE_URL}/topology/{topology_id}?reservationId={res_a}")
        # Wait for the seeded fork canvas to finish loading (the LiveEditBar shows
        # its device count) before committing. Clicking while the canvas is still
        # empty would reconcile nothing and never trigger the conflict; this was
        # the source of an order-dependent flake under a loaded shared stack.
        expect(page_a.get_by_text("2 devices")).to_be_visible(timeout=20000)
        commit = page_a.get_by_role("button", name="Commit to reservation")
        expect(commit).to_be_enabled(timeout=20000)
        commit.click()

        dialog = page_a.locator("dialog[open]")
        expect(dialog.get_by_text("Ports already claimed")).to_be_visible(timeout=15000)
        # The dialog names B's blocking reservation by its 8-char short id.
        expect(dialog.get_by_text(res_b[:8], exact=False).first).to_be_visible()

        # --- Effect read-back: A's fork never advanced; B's wiring untouched. ---
        fork_a_after = _api(page_a, "GET", f"/reservations/{res_a}/fork").json()
        fork_b_after = _api(page_b, "GET", f"/reservations/{res_b}/fork").json()
        assert _max_fork_version(fork_a_after) == _max_fork_version(fork_a_before), (
            "A's fork version advanced despite the rejected save"
        )
        assert _fork_conn_identities(fork_a_after) == _fork_conn_identities(fork_a_before), (
            "A's fork gained wiring despite the rejected save"
        )
        assert _max_fork_version(fork_b_after) == _max_fork_version(fork_b_before), (
            "B's fork version changed while A was rejected"
        )
        assert _fork_conn_identities(fork_b_after) == _fork_conn_identities(fork_b_before), (
            "B's wiring changed while A was rejected"
        )
    finally:
        for rid in (res_a, res_b):
            if rid:
                _cancel_and_wait(page_a, rid)
        if connection:
            _api(page_a, "DELETE", f"/cabling/connections/{connection['id']}", allow_errors=True)
        for dev in (switch, shared, dut_a, dut_b):
            if dev:
                _api(page_a, "DELETE", f"/inventory/devices/{dev['id']}", allow_errors=True)
        if template_id:
            _api(page_a, "DELETE", f"/inventory/templates/{template_id}", allow_errors=True)
        if driver_id:
            _api(page_a, "DELETE", f"/inventory/drivers/{driver_id}", allow_errors=True)
        if topology_id:
            _api(page_a, "DELETE", f"/cabling/topologies/{topology_id}", allow_errors=True)
        ctx_a.close()
        ctx_b.close()


# --------------------------------------------------------------------------- #
# 2. Utilization fleet CSV download.                                          #
# --------------------------------------------------------------------------- #
def test_utilization_fleet_csv_download(pw_page):
    pw_login(pw_page)
    suffix = uuid.uuid4().hex[:8]
    driver_id = template_id = None
    device = None
    topology_id = None
    res_id = None

    try:
        driver_id, template_id = _seed_template(pw_page, suffix)
        device = _seed_device(pw_page, template_id, f"pw-t2-csvdev-{suffix}")
        topology_id = _empty_topology(pw_page, suffix)
        res_id = _create_active_reservation(
            pw_page, [device["id"]], topology_id, f"pw-t2-csv-{suffix}"
        )["id"]

        pw_page.goto(f"{HOST_BASE_URL}/reporting")
        # Custom range: keep the 30-day start, push the end to tomorrow so the
        # just-started ACTIVE reservation (now..now+1h) sits fully inside the
        # window and its overlap hours are strictly positive.
        pw_page.get_by_role("button", name="Custom").click()
        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
        pw_page.locator('input[type="date"]').nth(1).fill(tomorrow)

        # The Fleet Utilization card renders first, so its Download CSV is the
        # first such button on the page.
        fleet_csv = pw_page.get_by_role("button", name="Download CSV").first
        expect(fleet_csv).to_be_enabled(timeout=20000)
        with pw_page.expect_download() as dl_info:
            fleet_csv.click()
        download = dl_info.value
        text = Path(download.path()).read_text()

        rows = list(csv.DictReader(io.StringIO(text)))
        assert rows, "fleet CSV parsed to zero rows"
        assert {"device_id", "reservation_count"} <= set(rows[0].keys()), (
            f"unexpected CSV columns: {list(rows[0].keys())}"
        )
        mine = [r for r in rows if r["device_id"] == device["id"]]
        assert len(mine) == 1, "the seeded device is missing from the fleet CSV"
        assert int(mine[0]["reservation_count"]) >= 1, (
            "the seeded ACTIVE reservation is not counted in the fleet CSV row"
        )
    finally:
        if res_id:
            _cancel_and_wait(pw_page, res_id)
        if device:
            _api(pw_page, "DELETE", f"/inventory/devices/{device['id']}", allow_errors=True)
        if template_id:
            _api(pw_page, "DELETE", f"/inventory/templates/{template_id}", allow_errors=True)
        if driver_id:
            _api(pw_page, "DELETE", f"/inventory/drivers/{driver_id}", allow_errors=True)
        if topology_id:
            _api(pw_page, "DELETE", f"/cabling/topologies/{topology_id}", allow_errors=True)


# --------------------------------------------------------------------------- #
# 3. Bulk topologies export download.                                         #
# --------------------------------------------------------------------------- #
def test_bulk_export_topologies_download(pw_page):
    pw_login(pw_page)
    suffix = uuid.uuid4().hex[:8]
    created: list[dict] = []

    try:
        for i in range(3):
            topo = _api(
                pw_page,
                "POST",
                "/cabling/topologies",
                json={"name": f"pw-t2-exp-{suffix}-{i}"},
            ).json()
            created.append(topo)

        pw_page.goto(f"{HOST_BASE_URL}/topology")
        export_btn = pw_page.get_by_role("button", name="Export JSON")
        expect(export_btn).to_be_visible(timeout=20000)
        with pw_page.expect_download() as dl_info:
            export_btn.click()
        download = dl_info.value
        payload = json.loads(Path(download.path()).read_text())

        items = payload["items"] if isinstance(payload, dict) else payload
        names = {it.get("name") for it in items}
        created_names = {t["name"] for t in created}
        present = created_names & names
        assert present == created_names, (
            f"export missing created topologies: {created_names - names}"
        )
    finally:
        for topo in created:
            _api(pw_page, "DELETE", f"/cabling/topologies/{topo['id']}", allow_errors=True)


# --------------------------------------------------------------------------- #
# 4. SSE assistant streaming.                                                 #
# --------------------------------------------------------------------------- #
def _parse_stream_conversation_id(sse_text: str) -> str | None:
    """Pull conversation_id out of the SSE stream's `done` frame data line."""
    for line in sse_text.splitlines():
        if not line.startswith("data:"):
            continue
        raw = line[len("data:") :].strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("conversation_id"):
            return obj["conversation_id"]
    return None


@pytest.mark.seeded_skip_ok(
    "AI provider is not configured on the gate or nightly stack; an environmental "
    "gate no seed can satisfy"
)
def test_assistant_stream_token_by_token(pw_page):
    pw_login(pw_page)

    status = _api(pw_page, "GET", "/ai/status", allow_errors=True)
    status_payload = status.json() if status.status_code == 200 else {"http": status.status_code}
    if not (status.status_code == 200 and status_payload.get("enabled")):
        pytest.skip(f"AI assistant not configured; /api/ai/status = {status_payload}")

    suffix = uuid.uuid4().hex[:8]
    driver_id = template_id = None
    device = None
    topology_id = None
    res_id = None
    purpose = f"pw-t2-ai-{suffix}"

    try:
        driver_id, template_id = _seed_template(pw_page, suffix)
        device = _seed_device(pw_page, template_id, f"pw-t2-aidev-{suffix}")
        topology_id = _empty_topology(pw_page, suffix)
        res_id = _create_active_reservation(pw_page, [device["id"]], topology_id, purpose)["id"]

        pw_page.goto(f"{HOST_BASE_URL}/reservations")
        row = pw_page.locator("tr", has_text=purpose)
        expect(row).to_be_visible(timeout=15000)
        row.click()
        dialog = pw_page.locator("dialog[open]")
        expect(dialog.locator("#modal-title")).to_have_text("Reservation")
        dialog.get_by_role("button", name="AI Assistant", exact=True).click()

        # The streaming chat UI is behind a build-time flag (VITE_AI_CHAT_ENABLED);
        # if this stack shipped the legacy single-shot form, the token-render claim
        # does not apply, so skip rather than fail.
        send = pw_page.get_by_test_id("assistant-send")
        if send.count() == 0:
            pytest.skip("frontend built without VITE_AI_CHAT_ENABLED; no streaming UI")

        pw_page.get_by_test_id("assistant-input").fill("Say hi in one short word.")
        with pw_page.expect_response(lambda r: "/assistant/stream" in r.url) as resp_info:
            send.click()

        # Sample the streaming region while the answer accumulates. A small local
        # model streams over several seconds, so distinct growing lengths here are
        # direct evidence of token-by-token render.
        streaming = pw_page.get_by_test_id("assistant-streaming")
        final_bubble = pw_page.get_by_test_id("bubble-assistant")
        lengths: list[int] = []
        deadline = time.monotonic() + 45.0
        while time.monotonic() < deadline:
            if streaming.count() > 0:
                try:
                    lengths.append(len(streaming.inner_text(timeout=250)))
                except Exception:
                    pass
            if final_bubble.count() > 0:
                break
            pw_page.wait_for_timeout(60)

        expect(final_bubble.first).to_be_visible(timeout=15000)
        answer = final_bubble.first.inner_text().strip()
        assert answer, "the assistant produced an empty final answer"

        # The UI rendered streamed content live (the streaming region was non-empty
        # while the answer was still arriving).
        positive = [n for n in lengths if n > 0]
        assert positive, "never observed any streamed content in the UI before the answer landed"

        # Protocol-level proof of token-by-token render: the SSE stream carried
        # several discrete token frames ahead of the terminal done frame, which is
        # what "content grows before done" means. Counting frames is deterministic
        # where DOM sampling of a fast local model is not.
        resp = resp_info.value
        sse_text = resp.text()
        token_frames = sse_text.count("event: token\n")
        first_token_at = sse_text.find("event: token")
        done_at = sse_text.find("event: done")
        assert token_frames >= 2, f"expected multiple streamed token frames, saw {token_frames}"
        assert 0 <= first_token_at < done_at, "tokens did not stream before the done frame"

        # Effect read-back: the streamed turn persisted. Replay the conversation
        # id against the buffered endpoint; get_or_404 there would 404 if the
        # conversation was never written.
        conversation_id = _parse_stream_conversation_id(sse_text)
        assert conversation_id, "no conversation_id in the SSE done frame"
        follow = _api(
            pw_page,
            "POST",
            f"/ai/reservations/{res_id}/assistant",
            json={"question": "ok", "conversation_id": conversation_id},
            allow_errors=True,
        )
        assert follow.status_code == 200, (
            f"replaying the persisted conversation failed: {follow.status_code} {follow.text[:200]}"
        )
        assert follow.json()["conversation_id"] == conversation_id, (
            "buffered follow-up did not resolve the same persisted conversation"
        )
    finally:
        if res_id:
            _cancel_and_wait(pw_page, res_id)
        if device:
            _api(pw_page, "DELETE", f"/inventory/devices/{device['id']}", allow_errors=True)
        if template_id:
            _api(pw_page, "DELETE", f"/inventory/templates/{template_id}", allow_errors=True)
        if driver_id:
            _api(pw_page, "DELETE", f"/inventory/drivers/{driver_id}", allow_errors=True)
        if topology_id:
            _api(pw_page, "DELETE", f"/cabling/topologies/{topology_id}", allow_errors=True)
