"""E2E tests for the topology editor page."""

import time
import uuid

import pytest
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from .conftest import api_request

WAIT = 15


@pytest.fixture(scope="module")
def transient_topology(logged_in_browser, base_url):
    """Create a topology via API and open its editor. Deletes after module."""
    # Navigate so the browser has a live session before we fetch the JWT.
    logged_in_browser.get(f"{base_url}/topology")
    WebDriverWait(logged_in_browser, WAIT).until(
        EC.presence_of_element_located((By.TAG_NAME, "body"))
    )

    name = f"e2e-topo-{uuid.uuid4().hex[:8]}"
    resp = api_request(
        logged_in_browser,
        "POST",
        "/cabling/topologies",
        json={"name": name},
        allow_errors=True,
    )
    if resp.status_code not in (200, 201):
        pytest.skip(f"could not create topology: {resp.status_code} {resp.text}")
    topology = resp.json()
    yield topology
    api_request(
        logged_in_browser,
        "DELETE",
        f"/cabling/topologies/{topology['id']}",
        allow_errors=True,
    )


def _open_editor(driver, base_url, topology_id):
    driver.get(f"{base_url}/topology/{topology_id}")
    WebDriverWait(driver, WAIT).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, ".react-flow"))
    )


def test_topology_editor_loads_canvas(logged_in_browser, base_url, transient_topology):
    """Topology editor renders the React Flow canvas."""
    _open_editor(logged_in_browser, base_url, transient_topology["id"])
    canvas = logged_in_browser.find_element(By.CSS_SELECTOR, ".react-flow")
    assert canvas.is_displayed()


def test_topology_editor_equipment_panel_visible(logged_in_browser, base_url, transient_topology):
    """The floating Equipment panel is rendered with search and filters."""
    _open_editor(logged_in_browser, base_url, transient_topology["id"])
    wait = WebDriverWait(logged_in_browser, WAIT)
    wait.until(EC.presence_of_element_located((By.ID, "eq-search")))
    assert logged_in_browser.find_element(By.ID, "eq-type-filter").is_displayed()
    assert logged_in_browser.find_element(By.ID, "eq-topo-filter").is_displayed()


def test_topology_editor_equipment_panel_collapses(
    logged_in_browser, base_url, transient_topology
):
    """Clicking the panel's toggle button collapses and re-expands it."""
    _open_editor(logged_in_browser, base_url, transient_topology["id"])
    wait = WebDriverWait(logged_in_browser, WAIT)
    wait.until(EC.presence_of_element_located((By.ID, "eq-search")))

    toggle = logged_in_browser.find_element(
        By.XPATH,
        "//span[normalize-space()='Equipment']/following-sibling::button",
    )
    toggle.click()
    time.sleep(0.3)
    assert logged_in_browser.find_elements(By.ID, "eq-search") == []

    toggle.click()
    wait.until(EC.presence_of_element_located((By.ID, "eq-search")))


def test_topology_editor_search_filters_palette(
    logged_in_browser, base_url, transient_topology
):
    """Typing in the palette search filters the device list."""
    _open_editor(logged_in_browser, base_url, transient_topology["id"])
    wait = WebDriverWait(logged_in_browser, WAIT)
    search = wait.until(EC.presence_of_element_located((By.ID, "eq-search")))
    search.clear()
    typed = "nonexistent-device-xyz"
    search.send_keys(typed)

    time.sleep(1.0)  # debounce via useDeferredValue
    body = logged_in_browser.find_element(By.TAG_NAME, "body").text.lower()
    assert "no devices found" in body or "nonexistent" not in body

    # Remove the text via keystrokes so React's onChange fires (element.clear()
    # alone does not). Then wait out the user-profile debounce (200ms) so the
    # empty value is persisted; otherwise later tests that rely on the
    # inventory list will see "No devices found" from the stale saved filter.
    from selenium.webdriver.common.keys import Keys

    search.send_keys(Keys.END)
    search.send_keys(Keys.BACKSPACE * len(typed))
    time.sleep(0.5)


def test_topology_editor_show_reserved_checkbox(
    logged_in_browser, base_url, transient_topology
):
    """The 'Show reserved resources' toggle exists and is checked by default."""
    _open_editor(logged_in_browser, base_url, transient_topology["id"])
    wait = WebDriverWait(logged_in_browser, WAIT)
    wait.until(EC.presence_of_element_located((By.ID, "eq-search")))

    label = logged_in_browser.find_element(
        By.XPATH,
        "//label[contains(., 'Show reserved')]",
    )
    checkbox = label.find_element(By.CSS_SELECTOR, "input[type='checkbox']")
    assert checkbox.is_selected()
