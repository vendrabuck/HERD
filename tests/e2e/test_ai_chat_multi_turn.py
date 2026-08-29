"""E2E test for Branch 3: multi-turn chat UI on the reservation detail modal.

Verifies the chat tab renders, accepts a typed question, and that the
backend round-trip surfaces an assistant bubble. Skipped when AI is not
configured (the assistant tab does not render in that case) AND when the
chat UI feature flag is off (the legacy single-shot UI renders instead).

The test asserts on UI elements, not model wording: model output is
non-deterministic but the chat-bubble + conversation_id contract is stable.
"""

import os
import time

import pytest
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

WAIT = 30


def _chat_ui_enabled() -> bool:
    """Match the frontend's VITE_AI_CHAT_ENABLED gate. The flag is build-time
    so this check reads the runtime env that the build was given via compose.
    """
    return os.environ.get("VITE_AI_CHAT_ENABLED", "").lower() == "true"


def _ai_provider_configured() -> bool:
    """Same gate as tests/integration/_ai_helpers.py: anthropic needs a key OR a
    base URL (a local Anthropic-compatible endpoint needs only the base URL);
    openai_compat needs a base URL."""
    if os.environ.get("AI_PROVIDER", "anthropic") == "openai_compat":
        return bool(os.environ.get("AI_BASE_URL"))
    return bool(os.environ.get("AI_API_KEY") or os.environ.get("AI_BASE_URL"))


def _open_reservations(driver, base_url):
    driver.get(f"{base_url}/reservations")
    WebDriverWait(driver, WAIT).until(
        EC.presence_of_element_located(
            (
                By.XPATH,
                "//table | //*[contains(text(), 'No reservations')]",
            )
        )
    )


def _click_reservation_row(driver):
    driver.find_element(By.CSS_SELECTOR, "tbody tr").click()


@pytest.mark.seeded_skip_ok(
    "AI provider is not configured on the gate or nightly stack; an environmental "
    "gate no seed can satisfy"
)
def test_chat_tab_renders_thread_and_input(admin_browser, base_url, transient_reservation):
    """Open the AI Assistant tab, type a question, send it, and assert a
    user bubble + assistant bubble appear. Asserts only on the chat
    contract (not on model wording).
    """
    if not _ai_provider_configured():
        pytest.skip("AI provider not configured; assistant tab is hidden")
    if not _chat_ui_enabled():
        pytest.skip("VITE_AI_CHAT_ENABLED is off on this build; legacy UI renders instead")

    _open_reservations(admin_browser, base_url)
    admin_browser.refresh()
    wait = WebDriverWait(admin_browser, WAIT)
    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "tbody tr")))

    _click_reservation_row(admin_browser)
    wait.until(
        EC.visibility_of_element_located((By.XPATH, "//button[normalize-space()='AI Assistant']"))
    ).click()

    # The chat thread + input render.
    input_el = wait.until(
        EC.visibility_of_element_located((By.CSS_SELECTOR, '[data-testid="assistant-input"]'))
    )
    wait.until(
        EC.visibility_of_element_located((By.CSS_SELECTOR, '[data-testid="assistant-thread"]'))
    )

    input_el.send_keys("What devices are in this reservation?")
    admin_browser.find_element(By.CSS_SELECTOR, '[data-testid="assistant-send"]').click()

    # User bubble appears immediately, assistant bubble after the round-trip.
    wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, '[data-testid="bubble-user"]')))
    # Assistant bubble can take a while against a real model; cap separately.
    long_wait = WebDriverWait(admin_browser, 120)
    long_wait.until(
        EC.visibility_of_element_located((By.CSS_SELECTOR, '[data-testid="bubble-assistant"]'))
    )

    # The conversation id pill rendered, confirming the round-trip captured it.
    wait.until(
        EC.visibility_of_element_located((By.XPATH, "//*[contains(text(), 'Conversation:')]"))
    )

    # Reset button is clickable now that there's a conversation.
    reset = admin_browser.find_element(By.CSS_SELECTOR, '[data-testid="assistant-reset"]')
    reset.click()
    time.sleep(0.3)
    # The Conversation: pill is gone after reset.
    pills = admin_browser.find_elements(By.XPATH, "//*[contains(text(), 'Conversation:')]")
    assert not pills, "reset should clear the conversation pill"
