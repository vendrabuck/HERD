"""E2E test for the topology clone UI flow.

GAPS coverage: "Clone topology UI: open menu, click clone, see new entry".
Creates a transient topology via API, navigates to /topology, clicks the row's
Clone button, fills the clone-name input, submits, and asserts the browser
navigates to the new topology editor, then reads the clone back through the API.
Cleans up both topologies after, the clone by name even if navigation failed.
"""

import uuid

import pytest
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from .conftest import api_request

WAIT = 20


@pytest.fixture
def source_topology(logged_in_browser, base_url):
    """Create a source topology via API; delete after test."""
    logged_in_browser.get(f"{base_url}/topology")
    WebDriverWait(logged_in_browser, WAIT).until(
        EC.presence_of_element_located((By.TAG_NAME, "body"))
    )
    name = f"e2e-clone-src-{uuid.uuid4().hex[:8]}"
    resp = api_request(
        logged_in_browser,
        "POST",
        "/cabling/topologies",
        json={"name": name},
        allow_errors=True,
    )
    if resp.status_code not in (200, 201):
        pytest.skip(f"create source failed: {resp.status_code}")
    topology = resp.json()
    yield topology
    api_request(
        logged_in_browser,
        "DELETE",
        f"/cabling/topologies/{topology['id']}",
        allow_errors=True,
    )


def test_topology_clone_via_ui(logged_in_browser, base_url, source_topology):
    """Clicking Clone on a topology row and confirming creates a copy."""
    src_name = source_topology["name"]
    src_id = source_topology["id"]

    # Open the topology list and find the source row.
    logged_in_browser.get(f"{base_url}/topology")
    wait = WebDriverWait(logged_in_browser, WAIT)
    wait.until(EC.presence_of_element_located((By.TAG_NAME, "table")))
    row = wait.until(
        EC.presence_of_element_located(
            (By.XPATH, f"//tr[.//td[contains(., '{src_name}')]]")
        )
    )

    # Click that row's Clone button. Use stopPropagation expectation: this
    # opens the modal rather than navigating to the editor.
    clone_btn = row.find_element(
        By.XPATH, ".//button[normalize-space()='Clone']"
    )
    clone_btn.click()

    # Modal appears with the clone-name input prefilled.
    name_input = wait.until(
        EC.element_to_be_clickable((By.ID, "clone-topology-name"))
    )
    assert name_input.is_displayed()
    prefilled = name_input.get_attribute("value") or ""
    assert "copy" in prefilled.lower() or src_name in prefilled

    new_name = f"e2e-clone-dst-{uuid.uuid4().hex[:8]}"
    try:
        name_input.clear()
        name_input.send_keys(new_name)

        # Submit the modal form.
        submit = logged_in_browser.find_element(
            By.XPATH,
            "//button[@type='submit' and (contains(., 'Clone') or contains(., 'Cloning'))]",
        )
        submit.click()

        # Successful clone navigates to /topology/{new_id}.
        wait.until(EC.url_matches(r".*/topology/[0-9a-fA-F-]{36}.*"))
        new_url = logged_in_browser.current_url
        assert "/topology/" in new_url
        assert src_id not in new_url, f"navigation did not move to the clone: {new_url}"

        # API read-back (issue #1148): the page it landed on is a stored topology
        # carrying the typed name, not just a URL of the right shape.
        new_id = new_url.rstrip("/").split("/topology/")[-1].split("?")[0]
        clone = api_request(logged_in_browser, "GET", f"/cabling/topologies/{new_id}").json()
        assert clone["id"] == new_id, clone
        assert clone["name"] == new_name, clone
    finally:
        # Clean up by name, so a clone is removed even when navigation failed.
        listed = api_request(
            logged_in_browser,
            "GET",
            "/cabling/topologies",
            params={"search": new_name},
            allow_errors=True,
        )
        if listed.status_code == 200:
            for item in listed.json().get("items", []):
                if item["name"] == new_name:
                    api_request(
                        logged_in_browser,
                        "DELETE",
                        f"/cabling/topologies/{item['id']}",
                        allow_errors=True,
                    )
