"""Playwright e2e tests for the config UI: the save path, asserted by effect.

First Playwright tests in the suite (adopted 2026-07-19; issue #388 item 1).
They use the pw_page fixture from conftest.py (plain playwright sync API; see
the pw_browser docstring for why the pytest-playwright plugin is not used)
and follow the effect-assertion discipline: every UI mutation is verified by
an API read-back, never by the UI's own acknowledgment alone. The config
Save and Restart bug (issue #373) survived precisely because the old tests
stopped at UI feedback.

Scope notes:
- The write surface is gated until the config password is rotated
  (issue #256). Dev and CI stacks set CONFIG_ADMIN_PASSWORD in
  docker-compose.override.yml, which counts as rotated; on a stack where
  /api/config/status reports password_changed false these tests skip.
- Save and Restart is deliberately NOT covered here: it bounces the stack,
  which would break every test after it. It needs a serial, last-in-run
  slot (tracked in issue #388).
- The round-trip edits LDAP_USER_FILTER, inert while AUTH_METHOD is local,
  and restores the exact prior settings afterward. Restoring VALUES is not
  the whole story: every save_config() deletes the config.bootstrapped
  marker, which would permanently promote config.json above the environment
  on a stack still in auto-bootstrapped mode (the PR #374 precedence
  ladder). The preserve_precedence_marker fixture records the marker's
  presence via docker compose exec before the test and recreates it after,
  so a local dev stack keeps its precedence mode. Against a remote stack
  (no docker access) the restore is skipped and the promotion side effect
  is accepted; CI's ephemeral stack does not care either way.
"""

import os
import re
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import expect

HOST_BASE_URL = os.environ.get("E2E_HOST_BASE_URL", "https://localhost")
CONFIG_PASSWORD = os.environ.get("CONFIG_ADMIN_PASSWORD") or "admin123!"

# Schema label for LDAP_USER_FILTER (services/config/app/config_schema.py).
FILTER_LABEL = "User Search Filter"
SENTINEL_FILTER = "(uid={username})(e2e-playwright-sentinel)"

# Container path of the precedence marker (services/config/app/config_store.py).
BOOTSTRAP_MARKER = "/data/herd-config/config.bootstrapped"
REPO_ROOT = Path(__file__).resolve().parents[2]


def _config_exec(cmd: str) -> "subprocess.CompletedProcess[str]":
    """Run a shell command inside the config container; rc 125/127 etc. on no docker."""
    return subprocess.run(
        ["docker", "compose", "exec", "-T", "config", "sh", "-c", cmd],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=30,
    )


@pytest.fixture()
def preserve_precedence_marker():
    """Keep the stack's config-precedence mode across the save round-trip.

    save_config() deletes the config.bootstrapped marker on every write (a
    UI save is a deliberate operator decision, PR #374); this test's save is
    not one, so if the marker existed before the test it is recreated after.
    Best-effort: when docker compose is unreachable (remote stack), the
    probe fails and nothing is restored.
    """
    probe = _config_exec(f"test -f {BOOTSTRAP_MARKER} && echo PRESENT || echo ABSENT")
    had_marker = probe.returncode == 0 and "PRESENT" in probe.stdout
    yield
    if had_marker:
        _config_exec(f"touch {BOOTSTRAP_MARKER}")


@pytest.fixture(scope="module")
def config_api():
    """Authenticated host-side client for the config API, or skip.

    Skips when the write surface is locked (unrotated password) or the
    password differs from CONFIG_ADMIN_PASSWORD/admin123!, so a prod-shaped
    stack fails soft instead of red.
    """
    with httpx.Client(base_url=HOST_BASE_URL, verify=False, timeout=15.0) as client:
        status = client.get("/api/config/status").json()
        if not status.get("password_changed"):
            pytest.skip("config password unrotated; write surface locked (issue #256 gate)")
        login = client.post("/api/config/login", json={"password": CONFIG_PASSWORD})
        if login.status_code != 200:
            pytest.skip("config password does not match CONFIG_ADMIN_PASSWORD fallback")
        client.headers["Authorization"] = f"Bearer {login.json()['token']}"
        yield client


def _ui_login(page):
    page.goto(f"{HOST_BASE_URL}/config")
    page.fill("#config-password", CONFIG_PASSWORD)
    page.get_by_role("button", name="Sign in").click()


def test_config_login_success_shows_editor(pw_page, config_api):
    """The config login SUCCESS path renders the editor (previously untested).

    The old suite covered only the unauthenticated render and the
    wrong-password toast; a login regression was invisible.
    """
    _ui_login(pw_page)
    expect(pw_page.get_by_role("button", name="Save", exact=True)).to_be_visible()
    expect(pw_page.get_by_role("button", name="Save and Restart")).to_be_visible()
    expect(pw_page.get_by_text("Configure all HERD service settings")).to_be_visible()


def test_config_save_persists_value_via_api_readback(
    pw_page, config_api, preserve_precedence_marker
):
    """Editing a field and clicking Save actually lands in config.json.

    UI action, then API read-back (GET /api/config/settings) as the effect
    assertion. Restores the exact prior settings dict afterward; PUT is a
    full replace and masked secret values resolve server-side (PR #374), so
    echoing the GET payload back is safe.
    """
    baseline = config_api.get("/api/config/settings").json()["values"]
    try:
        _ui_login(pw_page)
        field = pw_page.get_by_text(FILTER_LABEL, exact=True).locator(
            "xpath=following-sibling::input"
        )
        field.fill(SENTINEL_FILTER)
        pw_page.get_by_role("button", name="Save", exact=True).click()
        expect(pw_page.get_by_text("Configuration saved")).to_be_visible()

        saved = config_api.get("/api/config/settings").json()["values"]
        assert saved["LDAP_USER_FILTER"] == SENTINEL_FILTER
    finally:
        restore = config_api.put("/api/config/settings", json={"values": baseline})
        assert restore.status_code == 200
        restored = config_api.get("/api/config/settings").json()["values"]
        assert restored.get("LDAP_USER_FILTER") == baseline.get("LDAP_USER_FILTER")


def _filter_field(page):
    return page.get_by_text(FILTER_LABEL, exact=True).locator("xpath=following-sibling::input")


def test_config_full_cycle_edit_and_restore_via_ui(pw_page, config_api, preserve_precedence_marker):
    """Full config cycle where the RESTORE is also driven through the UI.

    Stronger than the API-restore round-trip above: after asserting the saved
    sentinel via the config API, this re-enters the original value through the
    same field and Save button, then asserts the API shows the baseline back.
    The finally block still full-replaces via the API as a safety net, because
    a UI restore that failed partway would leave a config.json edit that
    outranks the environment for the whole shared stack (the PR #374
    precedence ladder makes that restore load-bearing, not cosmetic).
    """
    baseline = config_api.get("/api/config/settings").json()["values"]
    original_filter = baseline.get("LDAP_USER_FILTER", "") or ""
    try:
        _ui_login(pw_page)

        _filter_field(pw_page).fill(SENTINEL_FILTER)
        pw_page.get_by_role("button", name="Save", exact=True).click()
        expect(pw_page.get_by_text("Configuration saved")).to_be_visible()
        saved = config_api.get("/api/config/settings").json()["values"]
        assert saved["LDAP_USER_FILTER"] == SENTINEL_FILTER

        # Restore the original value THROUGH THE UI, not the API.
        _filter_field(pw_page).fill(original_filter)
        pw_page.get_by_role("button", name="Save", exact=True).click()
        expect(pw_page.get_by_text("Configuration saved")).to_be_visible()
        restored = config_api.get("/api/config/settings").json()["values"]
        # Empty string is stored as unset (PR #374), so an originally-empty
        # value reads back as either "" or absent; both satisfy the baseline.
        assert (restored.get("LDAP_USER_FILTER") or "") == original_filter
    finally:
        config_api.put("/api/config/settings", json={"values": baseline})


@pytest.mark.skipif(
    os.environ.get("HERD_E2E_RESTART") != "1",
    reason="Save and Restart bounces the whole stack; opt in with HERD_E2E_RESTART=1",
)
def test_config_save_and_restart_gated(pw_page, config_api, preserve_precedence_marker):
    """Save and Restart: persists a value AND restarts services, asserted by effect.

    Gated behind HERD_E2E_RESTART=1 and MUST NOT run implicitly or concurrently
    with any other test or agent: clicking Save and Restart bounces every
    service in this compose project, which would break anything else in flight.
    Issue #388 item 1 flags it as needing a serial, last-in-run slot; this test
    is that slot. It is the case that would have caught the #373 restart bug.

    Effect assertions: the restart-result panel renders with a Restarted list,
    the config service comes back healthy (its status endpoint answers 200),
    and the edited value is readable via the config API afterward.
    """
    baseline = config_api.get("/api/config/settings").json()["values"]
    try:
        _ui_login(pw_page)
        _filter_field(pw_page).fill(SENTINEL_FILTER)

        pw_page.get_by_role("button", name="Save and Restart").click()

        # The restart-result panel is the UI acknowledgment (issue #373's blind
        # spot); the health poll and API read-back below are the real effect.
        expect(pw_page.get_by_text("Restart Result")).to_be_visible(timeout=90_000)
        expect(pw_page.get_by_text(re.compile(r"Restarted:"))).to_be_visible(timeout=90_000)

        deadline = time.monotonic() + 90.0
        healthy = False
        while time.monotonic() < deadline:
            try:
                with httpx.Client(base_url=HOST_BASE_URL, verify=False, timeout=10.0) as probe:
                    status = probe.get("/api/config/status")
                if status.status_code == 200 and status.json().get("configured"):
                    healthy = True
                    break
            except httpx.HTTPError:
                pass
            time.sleep(2.0)
        assert healthy, "config service did not report healthy after Save and Restart"

        saved = config_api.get("/api/config/settings").json()["values"]
        assert saved["LDAP_USER_FILTER"] == SENTINEL_FILTER
    finally:
        config_api.put("/api/config/settings", json={"values": baseline})
