"""Playwright e2e coverage for the topology editor's Routing panel (ADR 0014
phase 2, issue #34).

Creates a real Layer 3 Switch device (its own uploaded mock_l3 driver and
template, so this test does not depend on anything the seed script happens to
provide), gives it a config version with one interface carrying a prefixed
IP, cables it to a seeded DUT device, and opens a topology whose canvas
carries both. The Routing panel is exercised through genuine UI interaction
(select the switch node, fill and submit the Add-route form, Save); the
backend effect is proven via API READ-BACK (the standing e2e rule; see
CLAUDE.md's E2E section) rather than trusting the UI's own acknowledgment:
`GET /cabling/topologies/{id}` must show the saved node's `data.l3`. The
second half edits that route to an unparseable destination, saves again, and
asserts the resulting validation problem renders as the red route-count
badge on the canvas node and the "Routing intent has N problems" toast (E5).

Only the DUT device is seed-dependent (`dut_only=true`, AVAILABLE, exclusive,
at least one port); if none exists this skips with a clear reason. Every
other entity (driver, template, switch device, config version, connection,
topology) is created and torn down by this test.
"""

import io
import tarfile
import uuid
from pathlib import Path

import pytest
from playwright.sync_api import expect

from .conftest import HOST_BASE_URL, pw_api, pw_login

MOCK_L3_DIR = Path(__file__).resolve().parents[2] / "drivers" / "mock_l3"


def _mock_l3_tarball() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name in ("driver.py", "driver_metadata.json"):
            tf.add(MOCK_L3_DIR / name, arcname=name)
    return buf.getvalue()


def _find_available_dut(page):
    """One seeded, AVAILABLE, exclusive DUT device with at least one port
    (mirrors conftest.py's pw_two_devices_with_ports filter, single-device
    since only one DUT is needed here)."""
    resp = pw_api(
        page, "GET", "/inventory/devices?limit=100&dut_only=true", allow_errors=True
    )
    if resp.status_code != 200:
        return None
    payload = resp.json()
    items = payload.get("items", payload) if isinstance(payload, dict) else payload
    for d in items:
        if d.get("status") != "AVAILABLE" or not d.get("exclusive", True):
            continue
        ports = pw_api(page, "GET", f"/inventory/devices/{d['id']}/ports").json()
        if len(ports) >= 1:
            return d, ports[0]
    return None


@pytest.fixture
def l3_switch_setup(pw_page):
    """Uploads a uniquely-named mock_l3 driver/template, creates one Layer 3
    Switch device with a config version (one interface, a prefixed IP), finds
    a seeded DUT, and cables the two together. Yields the assembled dict;
    tears every created entity back down afterward regardless of outcome.
    """
    page = pw_page
    pw_login(page)

    dut_found = _find_available_dut(page)
    if not dut_found:
        pytest.skip("no seeded AVAILABLE/exclusive DUT device with a port for the L3 e2e")
    dut, dut_port = dut_found

    suffix = uuid.uuid4().hex[:8]
    driver_resp = pw_api(
        page,
        "POST",
        "/inventory/drivers",
        files={"file": ("mock_l3.tar.gz", _mock_l3_tarball(), "application/gzip")},
        data={
            "name": f"e2e-mock-l3-{suffix}",
            "connection_type": "Layer 3 Switch",
            "description": "e2e routing panel test driver",
        },
        allow_errors=True,
    )
    if driver_resp.status_code not in (200, 201):
        pytest.skip(
            f"could not upload the mock L3 driver: "
            f"{driver_resp.status_code} {driver_resp.text}"
        )
    driver = driver_resp.json()

    template_resp = pw_api(
        page,
        "POST",
        "/inventory/templates",
        json={
            "name": f"e2e-mock-l3-tmpl-{suffix}",
            "template_type": "device",
            "driver_id": driver["id"],
            "vendor": "E2EVendor",
            "model": "MockL3Switch",
            "sections": [
                {
                    "name": "General",
                    "fields": [{"key": "model", "label": "Model", "type": "string"}],
                }
            ],
        },
        allow_errors=True,
    )
    if template_resp.status_code not in (200, 201):
        pw_api(page, "DELETE", f"/inventory/drivers/{driver['id']}", allow_errors=True)
        pytest.skip(
            f"could not create the L3 template: "
            f"{template_resp.status_code} {template_resp.text}"
        )
    template = template_resp.json()

    device_resp = pw_api(
        page,
        "POST",
        "/inventory/devices",
        json={
            "name": f"e2e-l3-switch-{suffix}",
            "template_id": template["id"],
            "topology_type": "PHYSICAL",
            "status": "AVAILABLE",
            "field_data": {"model": "test"},
        },
        allow_errors=True,
    )
    if device_resp.status_code not in (200, 201):
        pw_api(page, "DELETE", f"/inventory/templates/{template['id']}", allow_errors=True)
        pw_api(page, "DELETE", f"/inventory/drivers/{driver['id']}", allow_errors=True)
        pytest.skip(
            f"could not create the L3 switch device: "
            f"{device_resp.status_code} {device_resp.text}"
        )
    switch = device_resp.json()

    config_resp = pw_api(
        page,
        "POST",
        f"/inventory/devices/{switch['id']}/config-versions",
        json={
            "config": {
                "interfaces": [{"name": "eth0", "ip": "10.20.0.1/24", "zone": "trust"}]
            },
            "description": "e2e routing panel test config",
        },
        allow_errors=True,
    )
    if config_resp.status_code not in (200, 201):
        pw_api(page, "DELETE", f"/inventory/devices/{switch['id']}", allow_errors=True)
        pw_api(page, "DELETE", f"/inventory/templates/{template['id']}", allow_errors=True)
        pw_api(page, "DELETE", f"/inventory/drivers/{driver['id']}", allow_errors=True)
        pytest.skip(
            f"could not create the switch config version: "
            f"{config_resp.status_code} {config_resp.text}"
        )

    # Every device auto-joins the "No Pool" default device group at create
    # time, but the seed script moves seeded devices into named groups
    # (removing them from "No Pool" as it goes). A freshly created switch
    # (still in "No Pool" only) and a seeded DUT (in some other, disjoint
    # group) therefore share no group, and cabling enforces device-group
    # boundaries (issue #392): join the switch into the DUT's first group so
    # the connection below is not refused as cross-group.
    dut_groups_resp = pw_api(page, "GET", f"/inventory/device-groups/device/{dut['id']}")
    dut_groups = dut_groups_resp.json()
    if dut_groups:
        pw_api(
            page,
            "POST",
            f"/inventory/device-groups/{dut_groups[0]['id']}/devices/bulk",
            json={"device_ids": [switch["id"]]},
            allow_errors=True,
        )

    connection_resp = pw_api(
        page,
        "POST",
        "/cabling/connections",
        json={
            "device_a_id": dut["id"],
            "port_a": dut_port["name"],
            "device_b_id": switch["id"],
            "port_b": "eth0",
            "connection_type": "L1",
        },
        allow_errors=True,
    )
    if connection_resp.status_code not in (200, 201):
        pw_api(page, "DELETE", f"/inventory/devices/{switch['id']}", allow_errors=True)
        pw_api(page, "DELETE", f"/inventory/templates/{template['id']}", allow_errors=True)
        pw_api(page, "DELETE", f"/inventory/drivers/{driver['id']}", allow_errors=True)
        pytest.skip(
            f"could not cable the switch to the DUT: "
            f"{connection_resp.status_code} {connection_resp.text}"
        )
    connection = connection_resp.json()

    yield {
        "dut": dut,
        "switch": switch,
        "driver": driver,
        "template": template,
        "connection": connection,
    }

    pw_api(page, "DELETE", f"/cabling/connections/{connection['id']}", allow_errors=True)
    pw_api(page, "DELETE", f"/inventory/devices/{switch['id']}", allow_errors=True)
    pw_api(page, "DELETE", f"/inventory/templates/{template['id']}", allow_errors=True)
    pw_api(page, "DELETE", f"/inventory/drivers/{driver['id']}", allow_errors=True)


def _canvas_with_switch_and_dut(dut_id: str, switch_id: str) -> dict:
    return {
        "nodes": [
            {
                "id": "n-dut",
                "type": "deviceNode",
                "position": {"x": 0, "y": 0},
                "data": {"device": {"id": dut_id}},
            },
            {
                "id": "n-switch",
                "type": "deviceNode",
                "position": {"x": 300, "y": 0},
                "data": {"device": {"id": switch_id}},
            },
        ],
        "edges": [
            {
                "id": "e1",
                "source": "n-dut",
                "target": "n-switch",
                "type": "layerEdge",
                "data": {"layer": "L1", "isProposal": False},
            }
        ],
    }


def test_add_route_saves_then_edit_to_bad_destination_shows_red_badge_and_toast(
    pw_page, l3_switch_setup
):
    page = pw_page
    setup = l3_switch_setup
    switch_id = setup["switch"]["id"]
    switch_name = setup["switch"]["name"]

    topo_resp = pw_api(
        page,
        "POST",
        "/cabling/topologies",
        json={"name": f"e2e-l3-routing-{uuid.uuid4().hex[:8]}"},
    )
    topology = topo_resp.json()
    put_resp = pw_api(
        page,
        "PUT",
        f"/cabling/topologies/{topology['id']}",
        json={"canvas_data": _canvas_with_switch_and_dut(setup["dut"]["id"], switch_id)},
    )
    assert put_resp.status_code == 200, put_resp.text

    page.goto(f"{HOST_BASE_URL}/topology/{topology['id']}")
    expect(page.locator(".react-flow")).to_be_visible(timeout=15_000)

    switch_node = page.locator(".react-flow__node").filter(has_text=switch_name)
    expect(switch_node).to_be_visible(timeout=15_000)
    switch_node.click()

    expect(page.get_by_text("Routing", exact=True)).to_be_visible(timeout=10_000)

    page.get_by_label("New destination").fill("10.20.1.0/24")
    page.get_by_label("New interface").fill("eth0")
    add_button = page.get_by_role("button", name="Add route")
    expect(add_button).to_be_enabled()
    add_button.click()

    page.get_by_role("button", name="Save", exact=True).click()
    expect(page.get_by_text("Topology saved")).to_be_visible(timeout=15_000)

    # Backend effect via API read-back (the standing e2e rule): the saved
    # canvas_data carries data.l3 on the switch node.
    saved = pw_api(page, "GET", f"/cabling/topologies/{topology['id']}").json()
    switch_node_data = next(
        n["data"] for n in saved["canvas_data"]["nodes"] if n["id"] == "n-switch"
    )
    expected_route = {
        "destination": "10.20.1.0/24",
        "next_hop": None,
        "interface": "eth0",
        "virtual_router": None,
    }
    assert switch_node_data.get("l3") == {"routes": [expected_route]}, switch_node_data

    # Edit the route to a bad destination and save again.
    # exact=True: "Destination" (the existing row) is otherwise a substring
    # match against "New destination" (the Add-route staging form) too.
    page.get_by_label("Destination", exact=True).fill("not-an-ip")
    page.get_by_role("button", name="Save", exact=True).click()

    expect(
        page.get_by_text("Routing intent has 1 problem", exact=False)
    ).to_be_visible(timeout=15_000)
    # exact=True: the toast text above also contains "l3_bad_destination" as a
    # substring, so this disambiguates to the Routing panel's own reason line.
    expect(page.get_by_text("l3_bad_destination", exact=True)).to_be_visible(timeout=10_000)

    badge = page.get_by_text("1 route", exact=True)
    expect(badge).to_be_visible()
    badge_class = badge.get_attribute("class") or ""
    assert "bg-red-600" in badge_class, badge_class

    pw_api(page, "DELETE", f"/cabling/topologies/{topology['id']}", allow_errors=True)
