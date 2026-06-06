"""E2E coverage for the topology connectivity validator UI.

Backend coverage already exists for the cabling validate endpoint and for the
reservation gate that calls it (`tests/integration/test_topology_validate_gate.py`).
This file proves the UX side: the topology editor visibly surfaces the
validation state and the Reserve Topology button is gated by it.

Topology canvas state is seeded via the API rather than through React Flow
drag-drop. Selenium against React Flow is brittle and slow; drag-and-drop
isn't what we're trying to verify here. The seeding path produces a real
topology row + canvas_data + connections, which is what the editor reads.
"""

import time
import uuid

import pytest
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from .conftest import api_request

WAIT = 20


def _canvas_with_edge(device_a: dict, device_b: dict) -> dict:
    """Canvas the editor will load: types must match the registered keys
    `deviceNode` / `layerEdge` from TopologyEditorPage.tsx."""
    return {
        "nodes": [
            {
                "id": "nA",
                "type": "deviceNode",
                "position": {"x": 100, "y": 100},
                "data": {"device": device_a},
            },
            {
                "id": "nB",
                "type": "deviceNode",
                "position": {"x": 400, "y": 100},
                "data": {"device": device_b},
            },
        ],
        "edges": [
            {
                "id": "e1",
                "source": "nA",
                "target": "nB",
                "type": "layerEdge",
                "data": {"layer": "L2", "isProposal": False},
            }
        ],
    }


@pytest.fixture(scope="module")
def two_seeded_devices(logged_in_browser, base_url):
    """Provision two fresh devices guaranteed to start with no cabling.

    The seed scripts produce a heavily-cabled fabric (5000+ devices, 7544+ L1
    connections), so two arbitrary picks are usually already reachable through
    the switch tier. Creating new devices skips the fabric entirely, which is
    what the negative test needs.
    """
    logged_in_browser.get(f"{base_url}/topology")
    WebDriverWait(logged_in_browser, WAIT).until(
        EC.presence_of_element_located((By.TAG_NAME, "body"))
    )

    tmpl_resp = api_request(
        logged_in_browser,
        "GET",
        "/inventory/templates?template_type=device&limit=1",
        allow_errors=True,
    )
    if tmpl_resp.status_code != 200:
        pytest.skip(f"could not list templates: {tmpl_resp.status_code} {tmpl_resp.text}")
    tmpls = tmpl_resp.json().get("items") or []
    if not tmpls:
        pytest.skip("validator UI tests need at least one device template")
    template_id = tmpls[0]["id"]

    created: list[dict] = []
    for i in range(2):
        body = {
            "name": f"e2e-validator-{uuid.uuid4().hex[:10]}",
            "template_id": template_id,
            "topology_type": "PHYSICAL",
            "status": "AVAILABLE",
            "field_data": {"ip": f"10.0.{i}.1", "login": "admin", "password": "admin123"},
        }
        resp = api_request(
            logged_in_browser, "POST", "/inventory/devices", json=body, allow_errors=True
        )
        if resp.status_code != 201:
            for d in created:
                api_request(
                    logged_in_browser,
                    "DELETE",
                    f"/inventory/devices/{d['id']}",
                    allow_errors=True,
                )
            pytest.skip(f"could not provision fresh device: {resp.status_code} {resp.text}")
        created.append(resp.json())

    yield created[0], created[1]

    for d in created:
        api_request(logged_in_browser, "DELETE", f"/inventory/devices/{d['id']}", allow_errors=True)


def _make_seeded_topology(driver, device_a: dict, device_b: dict) -> dict:
    name = f"e2e-validator-{uuid.uuid4().hex[:8]}"
    create = api_request(
        driver, "POST", "/cabling/topologies", json={"name": name}, allow_errors=True
    )
    if create.status_code not in (200, 201):
        pytest.skip(f"could not create topology: {create.status_code} {create.text}")
    topology = create.json()
    api_request(
        driver,
        "PUT",
        f"/cabling/topologies/{topology['id']}",
        json={"canvas_data": _canvas_with_edge(device_a, device_b)},
    )
    return topology


def _open_editor(driver, base_url, topology_id):
    driver.get(f"{base_url}/topology/{topology_id}")
    WebDriverWait(driver, WAIT).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, ".react-flow"))
    )


def _find_reserve_button(driver):
    return driver.find_element(
        By.XPATH, "//button[starts-with(normalize-space(.), 'Reserve Topology')]"
    )


def _wait_for_validation_to_settle(driver):
    """Wait until at least one of the validator's verdict labels is on screen.

    LayerEdge renders "no path" or "uncabled port" for invalid edges and
    "<N> hops" for reachable ones once /api/cabling/topologies/{id}/validate
    returns. Whichever appears first proves the fetch completed.
    """
    WebDriverWait(driver, WAIT).until(
        lambda d: any(
            label in d.find_element(By.TAG_NAME, "body").text
            for label in ("no path", "uncabled port", "hops")
        )
    )


def test_reserve_button_disabled_when_topology_has_unreachable_edges(
    logged_in_browser, base_url, two_seeded_devices
):
    """An L2 edge between two uncabled devices: 'no path' label + Reserve disabled."""
    a, b = two_seeded_devices
    topology = _make_seeded_topology(logged_in_browser, a, b)
    try:
        _open_editor(logged_in_browser, base_url, topology["id"])
        _wait_for_validation_to_settle(logged_in_browser)

        body_text = logged_in_browser.find_element(By.TAG_NAME, "body").text
        assert "no path" in body_text, (
            f"expected 'no path' label on the unreachable edge; body did not contain it.\n"
            f"first 500 chars: {body_text[:500]}"
        )

        button = _find_reserve_button(logged_in_browser)
        assert not button.is_enabled(), "Reserve Topology button should be disabled"
        title = button.get_attribute("title") or ""
        assert "Cannot reserve" in title, (
            f"expected disabled-button title to explain the gate, got: {title!r}"
        )
    finally:
        api_request(
            logged_in_browser,
            "DELETE",
            f"/cabling/topologies/{topology['id']}",
            allow_errors=True,
        )


def test_reserve_button_enabled_when_topology_edges_are_reachable(
    logged_in_browser, base_url, two_seeded_devices
):
    """Cabling the same device pair flips the verdict and re-enables Reserve."""
    a, b = two_seeded_devices
    cable = api_request(
        logged_in_browser,
        "POST",
        "/cabling/connections",
        json={
            "device_a_id": a["id"],
            "port_a": f"e2e-{uuid.uuid4().hex[:6]}",
            "device_b_id": b["id"],
            "port_b": f"e2e-{uuid.uuid4().hex[:6]}",
            "connection_type": "L1",
        },
        allow_errors=True,
    )
    if cable.status_code not in (200, 201):
        pytest.skip(f"could not seed cabling connection: {cable.status_code} {cable.text}")
    connection_id = cable.json()["id"]

    topology = _make_seeded_topology(logged_in_browser, a, b)
    try:
        _open_editor(logged_in_browser, base_url, topology["id"])
        _wait_for_validation_to_settle(logged_in_browser)

        # The label changes from "no path" to "<N> hops" once the fetch resolves.
        # Allow a small additional settle window because the React Query refetch
        # may race the canvas mount.
        deadline = time.time() + 5
        while time.time() < deadline:
            body_text = logged_in_browser.find_element(By.TAG_NAME, "body").text
            if "hops" in body_text and "no path" not in body_text:
                break
            time.sleep(0.3)
        body_text = logged_in_browser.find_element(By.TAG_NAME, "body").text
        assert "hops" in body_text, (
            f"expected '<N> hops' label on the reachable edge.\nfirst 500 chars: {body_text[:500]}"
        )
        assert "no path" not in body_text, "no edge should be labelled 'no path' here"

        button = _find_reserve_button(logged_in_browser)
        assert button.is_enabled(), "Reserve Topology button should be enabled"
    finally:
        api_request(
            logged_in_browser,
            "DELETE",
            f"/cabling/topologies/{topology['id']}",
            allow_errors=True,
        )
        api_request(
            logged_in_browser,
            "DELETE",
            f"/cabling/connections/{connection_id}",
            allow_errors=True,
        )
