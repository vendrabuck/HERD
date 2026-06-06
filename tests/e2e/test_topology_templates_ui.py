"""E2E tests for the Topology Templates page and instantiate-with-roles dialog.

GAPS coverage: "Template instantiate UI with role assignment". The page lives
at /topology-templates. When no templates exist we assert the empty-state copy;
when at least one template exists we exercise the instantiate dialog (open,
verify the role-assignment region, cancel) without committing because the live
role assignment requires devices of the right template to actually exist.

For full coverage we also seed a topology template via the API and walk
through "click Use, see role-to-device map" if any roles are present.
"""

import uuid

import pytest
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from .conftest import api_request

WAIT = 15
PAGE = "/topology-templates"


def _open_templates_page(driver, base_url):
    driver.get(f"{base_url}{PAGE}")
    WebDriverWait(driver, WAIT).until(
        EC.presence_of_element_located(
            (
                By.XPATH,
                "//h2[contains(., 'Topology Templates')]",
            )
        )
    )


def test_topology_templates_page_loads(admin_browser, base_url):
    """The /topology-templates page renders its header."""
    _open_templates_page(admin_browser, base_url)
    h2 = admin_browser.find_element(
        By.XPATH, "//h2[contains(., 'Topology Templates')]"
    )
    assert h2.is_displayed()


def test_topology_templates_page_shows_list_or_empty(admin_browser, base_url):
    """Either a table is rendered or the empty-state message appears."""
    _open_templates_page(admin_browser, base_url)
    WebDriverWait(admin_browser, WAIT).until(
        EC.presence_of_element_located(
            (
                By.XPATH,
                "//table | //*[contains(text(), 'No templates yet')]",
            )
        )
    )
    tables = admin_browser.find_elements(By.TAG_NAME, "table")
    empties = admin_browser.find_elements(
        By.XPATH, "//*[contains(text(), 'No templates yet')]"
    )
    assert tables or empties


@pytest.fixture
def seeded_topology_template(admin_browser, base_url):
    """Create a transient topology, save it as a template, return the template.

    Cleans up both the topology and the template afterwards.
    """
    # Make sure the browser has a live session so api_request can read the JWT.
    admin_browser.get(f"{base_url}/topology")
    WebDriverWait(admin_browser, WAIT).until(
        EC.presence_of_element_located((By.TAG_NAME, "body"))
    )

    topo_name = f"e2e-tmpl-src-{uuid.uuid4().hex[:8]}"
    topo_resp = api_request(
        admin_browser,
        "POST",
        "/cabling/topologies",
        json={"name": topo_name},
        allow_errors=True,
    )
    if topo_resp.status_code not in (200, 201):
        pytest.skip(f"create source topology failed: {topo_resp.status_code}")
    topology = topo_resp.json()

    tmpl_name = f"e2e-tmpl-{uuid.uuid4().hex[:8]}"
    tmpl_resp = api_request(
        admin_browser,
        "POST",
        f"/cabling/templates/from-topology/{topology['id']}",
        json={"name": tmpl_name, "description": "e2e fixture"},
        allow_errors=True,
    )
    if tmpl_resp.status_code not in (200, 201):
        api_request(
            admin_browser,
            "DELETE",
            f"/cabling/topologies/{topology['id']}",
            allow_errors=True,
        )
        pytest.skip(
            f"create template failed: {tmpl_resp.status_code} {tmpl_resp.text}"
        )
    template = tmpl_resp.json()

    yield template

    api_request(
        admin_browser,
        "DELETE",
        f"/cabling/templates/{template['id']}",
        allow_errors=True,
    )
    api_request(
        admin_browser,
        "DELETE",
        f"/cabling/topologies/{topology['id']}",
        allow_errors=True,
    )


def test_topology_template_instantiate_dialog_opens(
    admin_browser, base_url, seeded_topology_template
):
    """Clicking 'Use' on a template opens the instantiate dialog with a name input."""
    tmpl_name = seeded_topology_template["name"]
    _open_templates_page(admin_browser, base_url)
    wait = WebDriverWait(admin_browser, WAIT)
    row = wait.until(
        EC.presence_of_element_located(
            (By.XPATH, f"//tr[.//td[contains(., '{tmpl_name}')]]")
        )
    )
    row.find_element(By.XPATH, ".//button[normalize-space()='Use']").click()

    # Name input renders inside the modal.
    name_input = wait.until(
        EC.element_to_be_clickable((By.ID, "instantiate-name"))
    )
    assert name_input.is_displayed()

    # The role-assignment region is present even when there are no roles, the
    # component renders an explanatory message.
    body_text = admin_browser.find_element(By.TAG_NAME, "body").text
    assert "Role to device map" in body_text

    # Cancel without committing (no role assignments seeded). The page has
    # other hidden Cancel buttons (delete confirm dialog), so anchor on the
    # modal's submit button to disambiguate. Use a JS click to bypass any
    # off-screen warning for the first Cancel match.
    cancel = admin_browser.find_element(
        By.XPATH,
        "//button[normalize-space()='Create']"
        "/preceding-sibling::button[normalize-space()='Cancel']",
    )
    admin_browser.execute_script("arguments[0].click();", cancel)
