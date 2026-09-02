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

from .conftest import api_request, driver_tarball

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


def _log_if_failed(resource: str, resource_id: str, resp) -> None:
    """Print a non-2xx cleanup response instead of letting allow_errors=True hide it.

    Best-effort cleanup (allow_errors=True) must never mask the test's real
    failure by raising during teardown, but silently swallowing a non-2xx
    DELETE (a 409 from another service's reverse-reference guard, a 503 from
    a dependency being unreachable, ...) leaves the resource on the shared
    stack with no trace. This keeps the best-effort semantics and makes the
    leak visible in the run's output instead.
    """
    if resp.status_code // 100 != 2:
        print(f"cleanup left {resource} {resource_id} behind: {resp.status_code} {resp.text}")


def _cleanup_validator_fixtures(
    driver, device_ids: list[str], template_id: str | None, driver_id: str | None
) -> None:
    """Delete devices, then template, then driver: the dependency order every
    creation path below reverses. Called both from the fixture's post-yield
    teardown and from every mid-sequence failure path, so a partial seed
    never leaks past whatever was actually created.
    """
    for device_id in device_ids:
        resp = api_request(driver, "DELETE", f"/inventory/devices/{device_id}", allow_errors=True)
        _log_if_failed("device", device_id, resp)
    if template_id:
        resp = api_request(
            driver, "DELETE", f"/inventory/templates/{template_id}", allow_errors=True
        )
        _log_if_failed("template", template_id, resp)
    if driver_id:
        resp = api_request(driver, "DELETE", f"/inventory/drivers/{driver_id}", allow_errors=True)
        _log_if_failed("driver", driver_id, resp)


@pytest.fixture(scope="module")
def two_seeded_devices(logged_in_browser, base_url):
    """Provision two fresh devices guaranteed to start with no cabling.

    The seed scripts produce a heavily-cabled fabric (5000+ devices, 7544+ L1
    connections), so two arbitrary picks are usually already reachable through
    the switch tier. Creating new devices skips the fabric entirely, which is
    what the negative test needs.

    The device template is created here rather than picked from whatever the
    stack already has (issue #670): `tmpls[0]` depends on seed/list-order,
    and device create validates any field_data against the picked TEMPLATE's
    own field list (inventory_service.validate_field_data), not a fixed
    schema, so an arbitrary existing template's fields are unpredictable. A
    dedicated throwaway template with an empty field list sidesteps that
    entirely: field_data is simply omitted, since nothing here reads it back
    (only device id/name matter to the canvas and cabling calls below),
    mirroring the port template in test_connections_bulk_playwright.py's
    bulk_fixture_devices. The driver package itself is a no-op stub
    (conftest.driver_tarball): cabling/validate-safe only, since nothing here
    provisions through it.
    """
    logged_in_browser.get(f"{base_url}/topology")
    WebDriverWait(logged_in_browser, WAIT).until(
        EC.presence_of_element_located((By.TAG_NAME, "body"))
    )

    suffix = uuid.uuid4().hex[:10]
    driver_id: str | None = None
    template_id: str | None = None
    created: list[dict] = []

    files = {"file": ("e2e-validator-driver.tar.gz", driver_tarball(), "application/gzip")}
    data = {
        "name": f"e2e-validator-drv-{suffix}",
        "connection_type": "Management",
        "description": "topology validator e2e test driver",
    }
    driver_resp = api_request(
        logged_in_browser, "POST", "/inventory/drivers", files=files, data=data, allow_errors=True
    )
    if driver_resp.status_code != 201:
        pytest.skip(
            f"could not upload throwaway driver: {driver_resp.status_code} {driver_resp.text}"
        )
    driver_id = driver_resp.json()["id"]

    tmpl_resp = api_request(
        logged_in_browser,
        "POST",
        "/inventory/templates",
        json={
            "name": f"e2e-validator-tmpl-{suffix}",
            "template_type": "device",
            "driver_id": driver_id,
            "vendor": "e2e-validator",
            "model": "fixture",
            "sections": [{"name": "General", "fields": []}],
        },
        allow_errors=True,
    )
    if tmpl_resp.status_code != 201:
        _cleanup_validator_fixtures(logged_in_browser, [], None, driver_id)
        pytest.skip(
            f"could not create throwaway template: {tmpl_resp.status_code} {tmpl_resp.text}"
        )
    template_id = tmpl_resp.json()["id"]

    for _ in range(2):
        body = {
            "name": f"e2e-validator-{uuid.uuid4().hex[:10]}",
            "template_id": template_id,
            "topology_type": "PHYSICAL",
            "status": "AVAILABLE",
        }
        resp = api_request(
            logged_in_browser, "POST", "/inventory/devices", json=body, allow_errors=True
        )
        if resp.status_code != 201:
            _cleanup_validator_fixtures(
                logged_in_browser, [d["id"] for d in created], template_id, driver_id
            )
            pytest.skip(f"could not provision fresh device: {resp.status_code} {resp.text}")
        created.append(resp.json())

    yield created[0], created[1]

    _cleanup_validator_fixtures(
        logged_in_browser, [d["id"] for d in created], template_id, driver_id
    )


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
