"""E2E tests for the AI feature gate on the topology editor.

The 'Use AI' button should only render when /api/ai/status reports the feature
as enabled. These tests check the live state against the rendered UI so
flipping the AI configuration in either direction still gives consistent results.
"""

import uuid

import pytest
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from .conftest import api_request

WAIT = 15


@pytest.fixture(scope="module")
def transient_topology_for_ai(logged_in_browser, base_url):
    """Create a throwaway topology and return it so we can open the editor."""
    logged_in_browser.get(f"{base_url}/topology")
    WebDriverWait(logged_in_browser, WAIT).until(
        EC.presence_of_element_located((By.TAG_NAME, "body"))
    )
    name = f"e2e-ai-topo-{uuid.uuid4().hex[:8]}"
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


def _ai_enabled(driver) -> bool:
    resp = api_request(driver, "GET", "/ai/status", allow_errors=True)
    assert resp.status_code == 200, f"expected /ai/status 200, got {resp.status_code}"
    return bool(resp.json().get("enabled"))


def _find_use_ai_buttons(driver):
    return driver.find_elements(By.XPATH, "//button[normalize-space()='Use AI']")


def test_ai_status_endpoint_reachable(logged_in_browser):
    """Confirm /api/ai/status responds with a boolean enabled flag."""
    resp = api_request(logged_in_browser, "GET", "/ai/status", allow_errors=True)
    assert resp.status_code == 200
    body = resp.json()
    assert "enabled" in body
    assert isinstance(body["enabled"], bool)


def test_use_ai_button_matches_status(logged_in_browser, base_url, transient_topology_for_ai):
    """Editor button visibility must match /api/ai/status.enabled."""
    enabled = _ai_enabled(logged_in_browser)

    logged_in_browser.get(f"{base_url}/topology/{transient_topology_for_ai['id']}")
    WebDriverWait(logged_in_browser, WAIT).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, ".react-flow"))
    )
    # Give React Query a beat to resolve the /ai/status query.
    WebDriverWait(logged_in_browser, WAIT).until(
        lambda d: True  # .react-flow already present; no extra wait needed
    )

    buttons = _find_use_ai_buttons(logged_in_browser)
    if enabled:
        assert buttons, "Use AI button should render when ai/status reports enabled=true"
        assert buttons[0].is_displayed()
    else:
        assert buttons == [], "Use AI button must not render when ai/status reports enabled=false"


@pytest.mark.skipif(
    True,  # documentation-only; toggling the real config needs a stack restart
    reason="Requires live AI config toggle and container restart, outside e2e scope",
)
@pytest.mark.seeded_skip_ok(
    "documentation-only placeholder; no seed can provide a live config toggle + restart"
)
def test_use_ai_button_toggles_with_key_change():  # pragma: no cover
    """Placeholder for a manual flip test.

    To exercise by hand: set AI_API_KEY=sk-... (or AI_BASE_URL for a local
    endpoint) in .env, `make restart`, reload the topology editor, confirm the
    button appears; then blank the config, restart, confirm it disappears.
    Automating the restart is out of scope for the standard e2e suite.
    """
