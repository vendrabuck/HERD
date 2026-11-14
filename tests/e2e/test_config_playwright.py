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
- Save and Restart runs only with HERD_E2E_RESTART=1: it bounces every
  service that reads config.json, which would break every test after it.
  nightly.yml runs it alone, as its last test step (issue #1326).
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

import importlib.util
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import expect

from .conftest import pw_click_for_response, pw_toast_gone

HOST_BASE_URL = os.environ.get("E2E_HOST_BASE_URL", "https://localhost")
CONFIG_PASSWORD = os.environ.get("CONFIG_ADMIN_PASSWORD") or "admin123!"

# Schema label for LDAP_USER_FILTER (services/config/app/config_schema.py).
FILTER_LABEL = "User Search Filter"
SENTINEL_FILTER = "(uid={username})(e2e-playwright-sentinel)"

# The Save toast (ConfigPage.tsx handleSave). Matched exactly: Save and Restart
# raises "Configuration saved, restarting services...", which is another toast.
SAVED_TOAST = "Configuration saved"

# Container path of the precedence marker (services/config/app/config_store.py).
BOOTSTRAP_MARKER = "/data/herd-config/config.bootstrapped"
REPO_ROOT = Path(__file__).resolve().parents[2]


def _restart_allowlist() -> list[str]:
    """The restarter's RESTART_SERVICES (A), read from its module file.

    tests/unit/test_restarter_compose_guard.py pins A to the compose services that
    mount herd-config:/etc/herd:ro, so the panel must list exactly these.
    """
    path = REPO_ROOT / "infra" / "restarter" / "restarter.py"
    spec = importlib.util.spec_from_file_location("herd_restarter_e2e", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return sorted(module.RESTART_SERVICES)


# How long the services in the allowlist get to be running and healthy again after
# the Restart Result panel appears. Docker answers a restart once the container has
# started, not once its healthcheck passes; each service's healthcheck runs every 15 s
# with a start period of at most 10 s (docker-compose.yml).
RESTART_HEALTHY_SECONDS = 300.0
# The panel waits for the whole restart: the restarter's PASS_DEADLINE_SECONDS (660 s)
# under the config client's 720 s timeout, so a restarter that gives up shows as an
# error entry in the panel rather than as a Playwright timeout.
RESTART_PANEL_MS = 780_000


def _docker(*argv: str) -> str:
    """Run a docker CLI command on the host and return stdout; fail the test, never skip."""
    try:
        done = subprocess.run(
            ["docker", *argv], capture_output=True, text=True, cwd=REPO_ROOT, timeout=60
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        pytest.fail(f"docker {' '.join(argv[:3])} could not run on this host: {exc!r}")
    if done.returncode != 0:
        pytest.fail(f"docker {' '.join(argv[:3])} failed: {done.stderr.strip()[:300]}")
    return done.stdout


def _started_at(text: str) -> datetime:
    """Docker's StartedAt (RFC 3339, nanoseconds, Z) as an aware datetime."""
    stamp, _, fraction = text.rstrip("Z").partition(".")
    micro = (fraction + "000000")[:6]
    return datetime.fromisoformat(f"{stamp}.{micro}").replace(tzinfo=timezone.utc)


def _containers(services: list[str]) -> dict[str, dict]:
    """Every container of these compose services (stopped ones included), by id:
    its service, StartedAt, and health status ("none" without a healthcheck)."""
    ids = _docker("compose", "ps", "-a", "-q", *services).split()
    if not ids:
        return {}
    found = {}
    for item in json.loads(_docker("inspect", *ids)):
        state = item["State"]
        found[item["Id"]] = {
            "service": item["Config"]["Labels"].get("com.docker.compose.service"),
            "started": _started_at(state["StartedAt"]),
            "health": (state.get("Health") or {}).get("Status", "none"),
            "running": state.get("Running", False),
        }
    return found


def _not_back(services: list[str], before: dict[str, dict]) -> dict[str, list[str]]:
    """Services whose containers are not all restarted and healthy, with the reasons."""
    now = _containers(services)
    problems: dict[str, list[str]] = {}
    for container_id, old in before.items():
        new = now.get(container_id)
        reason = None
        if new is None:
            reason = f"container {container_id[:12]} is gone"
        elif not new["started"] > old["started"]:
            reason = f"container {container_id[:12]} was not restarted"
        elif not new["running"] or new["health"] != "healthy":
            reason = f"container {container_id[:12]} is {new['health']}"
        if reason:
            problems.setdefault(old["service"], []).append(reason)
    return problems


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


def _ui_save(page):
    """Click Save and return once THIS save's PUT has answered 200 and its toast is up.

    The read-back that follows is ordered by the PUT's own response (issue
    #1169): the config service writes config.json before it answers, so a GET
    that starts after this returns reads the saved value. The toast is a UI
    assertion only. It can be satisfied by nothing but this save's toast as
    long as no earlier "Configuration saved" toast is still in the page: true
    for the first save after a page load, and a test that saves a second time
    calls pw_toast_gone(page, SAVED_TOAST) first (a success toast stays 2 s).
    """
    pw_click_for_response(
        page,
        page.get_by_role("button", name="Save", exact=True),
        "PUT",
        "/api/config/settings",
    )
    expect(page.get_by_text(SAVED_TOAST, exact=True)).to_be_visible()


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
        _ui_save(pw_page)

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
        _ui_save(pw_page)
        saved = config_api.get("/api/config/settings").json()["values"]
        assert saved["LDAP_USER_FILTER"] == SENTINEL_FILTER

        # Restore the original value THROUGH THE UI, not the API. The first
        # save's toast carries the same text as the one this save will raise,
        # so it must be gone first: otherwise the toast check in _ui_save is
        # met by the stale toast, or by two toasts at once.
        pw_toast_gone(pw_page, SAVED_TOAST)
        _filter_field(pw_page).fill(original_filter)
        _ui_save(pw_page)
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
@pytest.mark.seeded_skip_ok(
    "deliberately opt-in only (HERD_E2E_RESTART=1); no seed makes this safe to run implicitly"
)
def test_config_save_and_restart_gated(pw_page, config_api, preserve_precedence_marker):
    """Save and Restart restarts exactly the allowlist, and every one of them returns.

    Gated behind HERD_E2E_RESTART=1 and MUST NOT run implicitly or concurrently
    with any other test or agent: clicking Save and Restart bounces every
    service that reads config.json, which would break anything else in flight.
    nightly.yml runs it alone as its last test step (issue #1326). It drives the
    real restart path, config to the restarter on herd-ctl to the Docker socket,
    and reads container state from the host's docker CLI, which it requires.

    Assertions, in order: before the click every service in the restarter's
    allowlist has at least one container; the Restart Result panel names exactly
    the allowlist, in sorted order, and renders no error line; within
    RESTART_HEALTHY_SECONDS every one of those containers has a StartedAt later
    than before the click (it was restarted) and is running and healthy, else the
    test fails naming each service that did not return and why; the saved value
    reads back through the config API.
    """
    allowlist = _restart_allowlist()
    expected = "Restarted: " + ", ".join(allowlist)
    before = _containers(allowlist)
    missing = sorted(set(allowlist) - {c["service"] for c in before.values()})
    assert not missing, f"no container before the click for {missing}"
    baseline = config_api.get("/api/config/settings").json()["values"]
    try:
        _ui_login(pw_page)
        _filter_field(pw_page).fill(SENTINEL_FILTER)

        pw_page.get_by_role("button", name="Save and Restart").click()

        # The panel reflects the restarter's answer, which Docker gives once each
        # container has started; the container poll below is the real effect.
        panel = pw_page.locator("div", has=pw_page.get_by_text("Restart Result", exact=True)).last
        expect(panel).to_be_visible(timeout=RESTART_PANEL_MS)
        expect(panel.get_by_text(expected, exact=True)).to_be_visible(timeout=10_000)
        # The errors list is red text under the Restarted line; none must render.
        expect(panel.locator("p.text-red-600, div.text-red-600")).to_have_count(0)

        deadline = time.monotonic() + RESTART_HEALTHY_SECONDS
        problems = _not_back(allowlist, before)
        while problems and time.monotonic() < deadline:
            time.sleep(3.0)
            problems = _not_back(allowlist, before)
        assert not problems, (
            f"services not restarted and healthy {RESTART_HEALTHY_SECONDS:.0f} s after the "
            f"panel: {problems}"
        )

        saved = config_api.get("/api/config/settings").json()["values"]
        assert saved["LDAP_USER_FILTER"] == SENTINEL_FILTER
    finally:
        config_api.put("/api/config/settings", json={"values": baseline})
