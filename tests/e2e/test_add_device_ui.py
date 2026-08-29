"""E2E tests for the admin Add Device page (/admin/add-device).

GAPS coverage: /admin/add-device was entirely untested; it is the default
Administration landing route (/admin redirects here) and the only UI path
for creating a device. These tests pin the page's toggle button ("Add
Device" / "Hide Form" rendered text), the create form's fields, and the full
create journey: pick a template, fill the template-driven dynamic fields,
submit, see the "Device created" toast, and verify the device through the
API. The created device is deleted via the API afterward so no state leaks
between runs.

The dynamic section of the form depends on which device templates the stack
has seeded; the create test fills text/password fields with a generic value
and number fields with a minimal one, and skips when no template exists.
"""

import time
import uuid

import pytest
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

from .conftest import api_request

WAIT = 15

# The toggle and the form submit share the "Add Device" label; only the
# submit carries type='submit', so the toggle is matched by excluding it.
TOGGLE_XPATH = (
    "//button[(normalize-space()='Add Device' and not(@type='submit'))"
    " or normalize-space()='Hide Form']"
)


def _open_add_device(driver, base_url):
    driver.get(f"{base_url}/admin/add-device")
    WebDriverWait(driver, WAIT).until(EC.element_to_be_clickable((By.XPATH, TOGGLE_XPATH)))


def _show_form(driver):
    toggle = driver.find_element(By.XPATH, TOGGLE_XPATH)
    if toggle.text.strip() == "Add Device":
        toggle.click()
    WebDriverWait(driver, WAIT).until(EC.visibility_of_element_located((By.ID, "dev-name")))


def test_add_device_page_toggles_form(admin_browser, base_url):
    """The toggle reveals the create form and flips its label to Hide Form."""
    _open_add_device(admin_browser, base_url)
    _show_form(admin_browser)

    toggle = admin_browser.find_element(By.XPATH, TOGGLE_XPATH)
    assert toggle.text.strip() == "Hide Form"

    for element_id in ("dev-name", "dev-template", "dev-topo", "dev-status", "dev-poll-interval"):
        assert admin_browser.find_element(By.ID, element_id).is_displayed(), (
            f"form field {element_id} not visible"
        )

    toggle.click()
    WebDriverWait(admin_browser, WAIT).until(
        EC.invisibility_of_element_located((By.ID, "dev-name"))
    )


def test_template_select_has_placeholder(admin_browser, base_url):
    """The template dropdown renders the placeholder option first."""
    _open_add_device(admin_browser, base_url)
    _show_form(admin_browser)

    select = Select(admin_browser.find_element(By.ID, "dev-template"))
    assert select.options, "template select rendered no options"
    assert select.options[0].text == "Select a template..."


def test_create_device_via_form(admin_browser, base_url):
    """Filling and submitting the form creates a device visible via the API."""
    _open_add_device(admin_browser, base_url)
    _show_form(admin_browser)

    # The template <select> renders its "Select a template..." placeholder
    # immediately, but the real, value-bearing options only appear once the
    # templates query resolves; without this wait the check below can read
    # the select before that query settles and skip on a stack that actually
    # has templates seeded (issue #629 review). A genuine absence of
    # templates still times out here and falls through to the existing skip.
    try:
        WebDriverWait(admin_browser, WAIT).until(
            lambda d: any(
                o.get_attribute("value")
                for o in Select(d.find_element(By.ID, "dev-template")).options
            )
        )
    except TimeoutException:
        pass

    select = Select(admin_browser.find_element(By.ID, "dev-template"))
    real_options = [o for o in select.options if o.get_attribute("value")]
    if not real_options:
        pytest.skip("no device templates seeded; cannot create a device via UI")

    name = f"e2e-add-device-{uuid.uuid4().hex[:10]}"
    name_input = admin_browser.find_element(By.ID, "dev-name")
    name_input.clear()
    name_input.send_keys(name)

    select.select_by_value(real_options[0].get_attribute("value"))

    # Fill the template-driven dynamic fields generically so required ones pass.
    form = admin_browser.find_element(By.XPATH, "//form[.//input[@id='dev-name']]")
    for field in form.find_elements(By.CSS_SELECTOR, "input[id^='field-']"):
        input_type = field.get_attribute("type")
        if input_type in ("text", "password"):
            field.clear()
            field.send_keys("e2e")
        elif input_type == "number":
            field.clear()
            field.send_keys("1")
    for sel in form.find_elements(By.CSS_SELECTOR, "select[id^='field-']"):
        dyn = Select(sel)
        non_empty = [o for o in dyn.options if o.get_attribute("value")]
        if non_empty:
            dyn.select_by_value(non_empty[0].get_attribute("value"))

    form.find_element(By.CSS_SELECTOR, "button[type='submit']").click()

    # Success path: the "Device created" toast renders and the form resets.
    toast_seen = False
    deadline = time.time() + WAIT
    while time.time() < deadline:
        body = admin_browser.find_element(By.TAG_NAME, "body").text
        if "Device created" in body:
            toast_seen = True
            break
        time.sleep(0.5)

    # Verify through the API regardless, then always clean up.
    created = None
    try:
        resp = api_request(
            admin_browser,
            "GET",
            f"/inventory/devices?search={name}&limit=50",
            allow_errors=True,
        )
        if resp.status_code == 200:
            payload = resp.json()
            items = payload.get("items", payload) if isinstance(payload, dict) else payload
            created = next((d for d in items if d.get("name") == name), None)

        assert toast_seen, "the 'Device created' toast never rendered"
        assert created is not None, "created device not found via the API"
        assert created["status"] == "AVAILABLE"
    finally:
        if created:
            api_request(
                admin_browser,
                "DELETE",
                f"/inventory/devices/{created['id']}",
                allow_errors=True,
            )
