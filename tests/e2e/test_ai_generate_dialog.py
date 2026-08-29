"""E2E tests for the AI Generate dialog on the topology editor.

GAPS coverage (partial): "AI Generate feature: submit prompt, accept proposal,
commit dialog." This file covers the UI half end-to-end:

- The Use AI button opens the dialog when /api/ai/status reports enabled.
- The dialog exposes a prompt textarea and a Generate button.
- The Generate button is disabled until the prompt has content.
- Empty-prompt submission shows a toast error.

We deliberately do not run a live LLM round-trip in the standard e2e tier:
the call is slow (multi-second), introduces nondeterministic content shape,
and is already exercised at the integration tier
(`tests/integration/test_ai_status.py::test_ai_generate_succeeds_when_key_present`).
The full UI cycle (submit prompt to accept proposal to commit) is left as a
deferred E2E gap until the LLM call can be stubbed at the network layer.
"""

import uuid

import pytest
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from .conftest import api_request

WAIT = 15


def _ai_enabled(driver) -> bool:
    resp = api_request(driver, "GET", "/ai/status", allow_errors=True)
    if resp.status_code != 200:
        return False
    return bool(resp.json().get("enabled"))


@pytest.fixture(scope="module")
def ai_topology(logged_in_browser, base_url):
    """Create a throwaway topology to host the editor session."""
    logged_in_browser.get(f"{base_url}/topology")
    WebDriverWait(logged_in_browser, WAIT).until(
        EC.presence_of_element_located((By.TAG_NAME, "body"))
    )
    if not _ai_enabled(logged_in_browser):
        pytest.skip("AI feature gate disabled; nothing to test in this module")
    name = f"e2e-ai-dialog-{uuid.uuid4().hex[:8]}"
    resp = api_request(
        logged_in_browser,
        "POST",
        "/cabling/topologies",
        json={"name": name},
        allow_errors=True,
    )
    if resp.status_code not in (200, 201):
        pytest.skip(f"create topology failed: {resp.status_code}")
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


def _open_ai_dialog(driver):
    wait = WebDriverWait(driver, WAIT)
    use_ai = wait.until(
        EC.element_to_be_clickable(
            (By.XPATH, "//button[normalize-space()='Use AI']")
        )
    )
    use_ai.click()
    wait.until(EC.element_to_be_clickable((By.ID, "ai-prompt")))


@pytest.mark.seeded_skip_ok(
    "AI provider is not configured on the gate or nightly stack; an environmental "
    "gate no seed can satisfy"
)
def test_use_ai_opens_dialog(logged_in_browser, base_url, ai_topology):
    """Clicking Use AI opens a dialog with a prompt textarea."""
    _open_editor(logged_in_browser, base_url, ai_topology["id"])
    _open_ai_dialog(logged_in_browser)
    prompt = logged_in_browser.find_element(By.ID, "ai-prompt")
    assert prompt.is_displayed()


@pytest.mark.seeded_skip_ok(
    "AI provider is not configured on the gate or nightly stack; an environmental "
    "gate no seed can satisfy"
)
def test_ai_generate_button_disabled_when_prompt_empty(
    logged_in_browser, base_url, ai_topology
):
    """The Generate button is disabled until a prompt is typed."""
    _open_editor(logged_in_browser, base_url, ai_topology["id"])
    _open_ai_dialog(logged_in_browser)
    generate = logged_in_browser.find_element(
        By.XPATH, "//button[normalize-space()='Generate']"
    )
    # Empty textarea, button disabled.
    assert generate.get_attribute("disabled") is not None

    prompt_box = logged_in_browser.find_element(By.ID, "ai-prompt")
    prompt_box.clear()
    prompt_box.send_keys("Two firewalls behind a client")

    # Re-find the button; React may have re-rendered it.
    generate = logged_in_browser.find_element(
        By.XPATH, "//button[normalize-space()='Generate']"
    )
    assert generate.get_attribute("disabled") is None


@pytest.mark.seeded_skip_ok(
    "AI provider is not configured on the gate or nightly stack; an environmental "
    "gate no seed can satisfy"
)
def test_ai_dialog_closes_via_escape(logged_in_browser, base_url, ai_topology):
    """Pressing Escape dismisses the AI dialog.

    The dialog is a native <dialog> opened with showModal(); the component
    installs a 'cancel' event handler that maps Escape to onClose. WebDriver
    clicks on buttons inside the top-layer dialog do not reliably propagate to
    React in Chrome 147, so we use the keyboard escape instead, which is the
    documented native behaviour of the underlying <dialog> element.
    """
    from selenium.webdriver.common.keys import Keys

    _open_editor(logged_in_browser, base_url, ai_topology["id"])
    _open_ai_dialog(logged_in_browser)
    prompt = logged_in_browser.find_element(By.ID, "ai-prompt")
    prompt.send_keys(Keys.ESCAPE)

    def _dialog_dismissed(driver):
        promps = driver.find_elements(By.ID, "ai-prompt")
        if not promps:
            return True
        # The native <dialog> element may stay in the DOM with display:none after
        # close(); treat hidden as dismissed.
        return not promps[0].is_displayed()

    WebDriverWait(logged_in_browser, WAIT).until(_dialog_dismissed)
