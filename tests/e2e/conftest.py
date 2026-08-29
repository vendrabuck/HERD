"""Selenium E2E test fixtures using Docker Selenium (standalone-chrome).

The Selenium container runs on the same Docker network as HERD,
so it reaches the app via the Traefik container hostname.
Tests connect to the remote WebDriver at http://localhost:4444.
"""

import os
import time
import urllib.error
import urllib.request
from pathlib import Path

import httpx
import pytest
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


def _load_repo_env() -> None:
    """Populate os.environ from the repo-root .env, matching docker-compose.

    docker-compose auto-loads .env when bringing up the stack, but pytest
    does not. Without this, the seeded admin (from .env SUPERADMIN_*) and
    the credentials this conftest tries to log in with can disagree.
    Existing env vars win so callers can still override per-run.
    """
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.is_file():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_repo_env()

# Base URL as seen by the Selenium container (inside Docker network).
BASE_URL = os.environ.get("E2E_BASE_URL", "https://traefik")

# Base URL as seen by the pytest process (host-side). Used for direct API calls
# made by test fixtures to seed or tear down data without going through the browser.
HOST_BASE_URL = os.environ.get("E2E_HOST_BASE_URL", "https://localhost")

# Selenium Hub URL (exposed to host via port mapping).
SELENIUM_HUB = os.environ.get("SELENIUM_HUB", "http://localhost:4444/wd/hub")

# Admin credentials. Fall back through E2E_* (per-run override) to SUPERADMIN_*
# (matches what the stack actually seeds from .env) to a generic default.
ADMIN_EMAIL = os.environ.get("E2E_EMAIL") or os.environ.get("SUPERADMIN_EMAIL", "admin@example.com")
ADMIN_PASSWORD = os.environ.get("E2E_PASSWORD") or os.environ.get(
    "SUPERADMIN_PASSWORD", "admin123!"
)

WAIT_TIMEOUT = 15

# -- Seeded-stack skip gate (issue #629) --------------------------------------
#
# make test-e2e runs before the gate stack is seeded (both in the everything
# recipe and in nightly.yml), so every test gated on an available device
# (transient_reservation above, pw_two_devices_with_ports, the fork tests'
# _pw_create_reserved_topology helper, ...) always skips there by design; that
# pass exercises the empty-stack UI paths instead. HERD_E2E_REQUIRE_NO_SKIP=1
# (set by `make test-e2e-seeded`) re-runs the identical suite against a
# seeded stack and turns any remaining skip into a failure, so a silent skip
# regression on the seeded pass shows up red instead of a quiet pass.
HERD_E2E_REQUIRE_NO_SKIP = "HERD_E2E_REQUIRE_NO_SKIP"

# A test carrying @pytest.mark.seeded_skip_ok("reason") is expected to keep
# skipping even on a seeded stack (an environmental gate the seed can't
# satisfy, e.g. AUTH_METHOD=local, or a deliberately opt-in/manual case). Its
# skip is excluded from the failing count but still surfaced, in a separate
# "exempt" block, so the log doesn't hide it. Registered in the root
# pyproject.toml `markers` list; filterwarnings=["error"] would otherwise turn
# the unregistered-marker warning into a collection error.
_exempt_reasons: dict[str, str] = {}
_skip_reports: list[tuple[str, str]] = []
_exempt_reports: list[tuple[str, str]] = []


def _skip_reason(report: pytest.TestReport) -> str:
    """Best-effort human-readable reason from a skipped test's longrepr."""
    longrepr = report.longrepr
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        return str(longrepr[2])
    if longrepr:
        return str(longrepr)
    return "skipped"


def format_skip_block(skips: list[tuple[str, str]]) -> str:
    """Render collected (nodeid, reason) pairs as the failure-mode report block.

    Pure and stack-free (no pytest hook machinery) so it can be unit tested
    directly, in tests/unit/test_e2e_seed_gate.py, without importing this
    module's Selenium/Playwright fixtures. Unchanged by the seeded_skip_ok
    exemption: callers pass only the non-exempt skips in here.
    """
    lines = [f"HERD_E2E_REQUIRE_NO_SKIP=1: {len(skips)} test(s) skipped on a seeded stack:"]
    for nodeid, reason in skips:
        lines.append(f"  {nodeid}: {reason}")
    return "\n".join(lines)


def format_exempt_block(exempt: list[tuple[str, str]]) -> str:
    """Render seeded_skip_ok-exempted skips as their own labeled block.

    Kept separate from format_skip_block (which stays unchanged) so an
    exempted skip never inflates the failing count; this is visibility only,
    an empty list renders as the empty string so callers can skip it cleanly.
    """
    if not exempt:
        return ""
    lines = ["exempt (seeded_skip_ok):"]
    for nodeid, reason in exempt:
        lines.append(f"  {nodeid}: {reason}")
    return "\n".join(lines)


def format_sessionfinish_report(skips: list[tuple[str, str]], exempt: list[tuple[str, str]]) -> str:
    """Render the full sessionfinish log output from the two report lists.

    Pure and stack-free, mirroring format_skip_block/format_exempt_block, so
    it can be unit tested directly. Returns the empty string when both lists
    are empty (nothing to print). The failing block (format_skip_block) is
    included only when skips is non-empty, so an all-exempt run prints just
    the exempt block rather than a "0 test(s) skipped" header with nothing
    under it.
    """
    if not skips and not exempt:
        return ""
    blocks = []
    if skips:
        blocks.append(format_skip_block(skips))
    exempt_block = format_exempt_block(exempt)
    if exempt_block:
        blocks.append(exempt_block)
    return "\n" + "\n\n".join(blocks) + "\n"


def pytest_collection_modifyitems(config: pytest.Config, items: list) -> None:
    """Record which collected items carry @pytest.mark.seeded_skip_ok.

    Runs regardless of HERD_E2E_REQUIRE_NO_SKIP; it only populates a lookup
    table that pytest_runtest_logreport consults, so it is cheap and inert
    when the gate itself is off.
    """
    for item in items:
        marker = item.get_closest_marker("seeded_skip_ok")
        if marker is None:
            continue
        reason = marker.args[0] if marker.args else marker.kwargs.get("reason", "")
        _exempt_reasons[item.nodeid] = str(reason)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Collect every skipped test, including setup-phase skips.

    A test skipped by a fixture (most of this gate's cases: transient_reservation,
    pw_two_devices_with_ports, _pw_create_reserved_topology) reports skipped at
    "setup"; a test that calls pytest.skip() itself reports at "call". Only one
    of the two phases is ever skipped for a given test, so no dedup is needed.
    A node id recorded in _exempt_reasons (seeded_skip_ok) routes to the
    exempt list instead of the failing one.
    """
    if not (report.skipped and report.when in ("setup", "call")):
        return
    entry = (report.nodeid, _skip_reason(report))
    if report.nodeid in _exempt_reasons:
        _exempt_reports.append(entry)
    else:
        _skip_reports.append(entry)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Fail the whole run if HERD_E2E_REQUIRE_NO_SKIP=1 and anything non-exempt skipped.

    Inert (a no-op) without the env var, so a plain `make test-e2e` run is
    unaffected. seeded_skip_ok exemptions never fail the run and are always
    printed when present; the failing block is printed only when at least
    one non-exempt skip occurred, so an all-exempt run doesn't show a
    "0 test(s) skipped" header with nothing under it.
    """
    if os.environ.get(HERD_E2E_REQUIRE_NO_SKIP) != "1":
        return
    output = format_sessionfinish_report(_skip_reports, _exempt_reports)
    if not output:
        return
    print(output)
    if _skip_reports:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def _make_chrome_options():
    options = Options()
    options.add_argument("--ignore-certificate-errors")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    return options


def _wait_for_selenium(url, retries=12, delay=5):
    """Wait for the Selenium Hub to be reachable."""
    status_url = url.replace("/wd/hub", "/wd/hub/status")
    for i in range(retries):
        try:
            resp = urllib.request.urlopen(status_url, timeout=5)
            if resp.status == 200:
                return
        except (urllib.error.URLError, OSError):
            pass
        if i < retries - 1:
            time.sleep(delay)
    raise RuntimeError(f"Selenium Hub not reachable at {status_url} after {retries} attempts")


def _do_login(driver, url, email, password):
    """Navigate to login page and authenticate."""
    driver.get(f"{url}/login")
    wait = WebDriverWait(driver, WAIT_TIMEOUT)

    email_input = wait.until(EC.presence_of_element_located((By.ID, "login-email")))
    password_input = driver.find_element(By.ID, "login-password")

    email_input.clear()
    email_input.send_keys(email)
    password_input.clear()
    password_input.send_keys(password)

    submit = driver.find_element(By.CSS_SELECTOR, "button[type='submit']")
    submit.click()

    wait.until(EC.url_contains("/topology"))


def _get_jwt_from_browser(driver):
    """Extract the current access token from the browser's localStorage.

    HERD stores the access token under the key `access_token`; refresh under
    `refresh_token`. Returns None if not logged in.
    """
    return driver.execute_script("return window.localStorage.getItem('access_token');")


def api_request(driver, method, path, **kwargs):
    """Make an authenticated HERD API request from the pytest process.

    Uses the host-side HERD URL (not the Selenium-visible one). Pulls the JWT
    from the browser's localStorage so the call runs as whichever user the
    browser is logged in as.

    Raises on non-2xx unless `allow_errors=True` is passed.
    """
    allow_errors = kwargs.pop("allow_errors", False)
    token = _get_jwt_from_browser(driver)
    headers = kwargs.pop("headers", {}) or {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"{HOST_BASE_URL}/api{path}"
    with httpx.Client(verify=False, timeout=30.0) as client:
        resp = client.request(method, url, headers=headers, **kwargs)
    if not allow_errors:
        resp.raise_for_status()
    return resp


def open_admin_menu(driver):
    """Open the Administration dropdown in the header nav.

    Tolerant of the button being labeled either "Administration" or "Admin".
    Returns when the dropdown menu is visible.
    """
    wait = WebDriverWait(driver, WAIT_TIMEOUT)
    trigger = wait.until(
        EC.element_to_be_clickable(
            (
                By.XPATH,
                "//button[contains(., 'Administration') or contains(., 'Admin')]",
            )
        )
    )
    trigger.click()
    # Any link under the revealed menu confirms it opened.
    wait.until(
        EC.visibility_of_element_located(
            (By.XPATH, "//a[contains(@href, '/admin/') or contains(@href, '/templates')]")
        )
    )


@pytest.fixture(scope="session")
def pw_browser():
    """Playwright Chromium (headless), shared across the session.

    Plain playwright sync API rather than the pytest-playwright plugin: the
    plugin's `browser` fixture chain is shadowed by this conftest's Selenium
    `browser` fixture (a local conftest fixture overrides same-named plugin
    fixtures for everything under tests/e2e/), so the plugin's page/context
    fixtures would receive a Selenium WebDriver and crash. The pw_ prefix
    keeps the two stacks side by side. Playwright runs in the pytest process
    on the HOST, so its tests target HOST_BASE_URL, not the in-Docker
    BASE_URL the Selenium container uses.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        yield browser
        browser.close()


@pytest.fixture
def pw_page(pw_browser):
    """Fresh Playwright context and page per test, trusting the dev TLS cert."""
    context = pw_browser.new_context(ignore_https_errors=True)
    page = context.new_page()
    yield page
    context.close()


def pw_login(page, email: str = ADMIN_EMAIL, password: str = ADMIN_PASSWORD) -> None:
    """Log in through the main app UI (Playwright), waiting for the post-login redirect.

    Shared by the Playwright test modules under issue #388 (groups, connections,
    templates, roles). Uses the same #login-email/#login-password/submit ids as
    the Selenium `_do_login` above so both stacks exercise the identical login
    form; kept as a plain function (not a fixture) since each test needs to
    choose its own pw_page and, for the roles test, log in as a specific user.
    """
    page.goto(f"{HOST_BASE_URL}/login")
    page.fill("#login-email", email)
    page.fill("#login-password", password)
    page.click("button[type='submit']")
    page.wait_for_url("**/topology**")


def pw_token(page) -> str | None:
    """The current access token from the browser's localStorage (Playwright)."""
    return page.evaluate("() => window.localStorage.getItem('access_token')")


def pw_api(page, method, path, **kwargs):
    """Authenticated host-side HERD API request, using the browser's own JWT (Playwright).

    Shared by every Playwright module that needs host-side API calls under
    the browser's own session (issue #517 review item 10 moved this out of
    test_connections_playwright.py, the original single owner, so
    test_wiring_dialog_playwright.py does not duplicate it).
    """
    allow_errors = kwargs.pop("allow_errors", False)
    token = pw_token(page)
    headers = kwargs.pop("headers", {}) or {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"{HOST_BASE_URL}/api{path}"
    with httpx.Client(verify=False, timeout=30.0) as client:
        resp = client.request(method, url, headers=headers, **kwargs)
    if not allow_errors:
        resp.raise_for_status()
    return resp


def pw_two_devices_with_ports(page, min_ports: int = 1):
    """Find two distinct seeded devices, of the SAME topology_type, that each
    have at least min_ports ports.

    Scans devices via the inventory API (the same data ConnectionsPage's
    device search and the wiring dialog both hit) until two qualifying
    devices are found, capped so a slow or empty inventory skips rather than
    hangs. Must be called after login: it needs the browser's JWT to call the
    API. Returns ((device_a, ports_a), (device_b, ports_b)); a caller that
    only needs one port per device (min_ports=1, the default) indexes
    ports_a[0]/ports_b[0].

    Matching topology_type is required (issue #517 review round 3 item
    12.10): TopologyEditorPage's isValidConnection refuses to even open the
    wiring surface for a PHYSICAL-to-CLOUD draw ("topology types must
    match"), so a mismatched pair would make any test built on this helper
    fail before it reached the behavior under test.
    """
    devices = pw_api(page, "GET", "/inventory/devices", params={"skip": 0, "limit": 100}).json()[
        "items"
    ]
    by_type: dict[str, list] = {}
    for d in devices:
        ports = pw_api(page, "GET", f"/inventory/devices/{d['id']}/ports").json()
        if len(ports) < min_ports:
            continue
        bucket = by_type.setdefault(d["topology_type"], [])
        bucket.append((d, ports))
        if len(bucket) >= 2:
            return bucket[0], bucket[1]
    pytest.skip(
        f"fewer than two seeded devices of the same topology_type with at least "
        f"{min_ports} port(s) available"
    )


@pytest.fixture(scope="session", autouse=True)
def _ensure_config_seeded():
    """Seed the config service so LoginPage isn't gated on the setup wizard.

    The LoginPage disables its inputs when `/api/config/status` returns
    `configured: false`, which is the case on a fresh volume even when `.env`
    already drives the backend. We POST directly to the config service's
    /login + /settings endpoints to materialize config.json; no /apply is
    needed since the stack is already running with the right env values.

    The seeded values are read from the test process env (populated from the
    repo-root .env above) so they match what the stack is running with: a
    config-UI save outranks the environment at runtime, so seeding hardcoded
    defaults would poison a stack whose .env differs.
    """
    with httpx.Client(base_url=HOST_BASE_URL, verify=False, timeout=15.0) as client:
        try:
            status_resp = client.get("/api/config/status")
            if status_resp.status_code == 200 and status_resp.json().get("configured"):
                return
            login_resp = client.post("/api/config/login", json={"password": "admin123!"})
            if login_resp.status_code != 200:
                return
            token = login_resp.json()["token"]
            headers = {"Authorization": f"Bearer {token}"}
            values = {
                "POSTGRES_USER": os.environ.get("POSTGRES_USER", "herd"),
                "POSTGRES_PASSWORD": os.environ.get("POSTGRES_PASSWORD", "herdpassword"),
                "POSTGRES_DB": os.environ.get("POSTGRES_DB", "herd"),
                "AUTH_SECRET_KEY": os.environ.get(
                    "AUTH_SECRET_KEY", "change-me-in-production-use-a-long-random-string"
                ),
                "INTERNAL_API_TOKEN": os.environ.get(
                    "INTERNAL_API_TOKEN", "change-me-internal-token"
                ),
            }
            client.put(
                "/api/config/settings",
                json={"values": values},
                headers=headers,
            )
        except httpx.HTTPError:
            # Don't block E2E if config service is unreachable; tests will
            # surface the real issue.
            pass


@pytest.fixture(scope="session")
def base_url():
    return BASE_URL


@pytest.fixture(scope="session")
def host_base_url():
    return HOST_BASE_URL


@pytest.fixture(scope="module")
def browser(base_url):
    """Module-scoped remote Chrome WebDriver (not logged in)."""
    _wait_for_selenium(SELENIUM_HUB)
    options = _make_chrome_options()
    driver = webdriver.Remote(command_executor=SELENIUM_HUB, options=options)
    driver.implicitly_wait(5)
    yield driver
    driver.quit()


@pytest.fixture(scope="module")
def logged_in_browser(base_url):
    """Browser logged in as admin (always available, no seed needed)."""
    _wait_for_selenium(SELENIUM_HUB)
    options = _make_chrome_options()
    driver = webdriver.Remote(command_executor=SELENIUM_HUB, options=options)
    driver.implicitly_wait(5)
    _do_login(driver, base_url, ADMIN_EMAIL, ADMIN_PASSWORD)
    yield driver
    driver.quit()


@pytest.fixture(scope="module")
def admin_browser(base_url):
    """Browser session logged in as admin."""
    _wait_for_selenium(SELENIUM_HUB)
    options = _make_chrome_options()
    driver = webdriver.Remote(command_executor=SELENIUM_HUB, options=options)
    driver.implicitly_wait(5)
    _do_login(driver, base_url, ADMIN_EMAIL, ADMIN_PASSWORD)
    yield driver
    driver.quit()


@pytest.fixture(scope="session")
def admin_credentials():
    """Return admin email and password."""
    return ADMIN_EMAIL, ADMIN_PASSWORD


@pytest.fixture
def transient_reservation(admin_browser):
    """Create a short reservation via API, yield it, then best-effort cancel it.

    Picks the first available exclusive device. Skips the test if none exist.
    """
    from datetime import datetime, timedelta, timezone

    devices_resp = api_request(
        admin_browser,
        "GET",
        "/inventory/devices?limit=100&dut_only=true",
        allow_errors=True,
    )
    if devices_resp.status_code != 200:
        pytest.skip(f"cannot list devices: {devices_resp.status_code}")

    payload = devices_resp.json()
    items = payload.get("items", payload) if isinstance(payload, dict) else payload
    available = [d for d in items if d.get("status") == "AVAILABLE" and d.get("exclusive", True)]
    if not available:
        pytest.skip("no available exclusive devices to reserve")

    now = datetime.now(timezone.utc)
    body = {
        "device_ids": [available[0]["id"]],
        "purpose": "e2e transient reservation",
        "start_time": now.isoformat(),
        "end_time": (now + timedelta(minutes=30)).isoformat(),
    }
    create = api_request(admin_browser, "POST", "/reservations/", json=body, allow_errors=True)
    if create.status_code != 201:
        pytest.skip(f"reservation create failed: {create.status_code} {create.text}")
    reservation = create.json()

    yield reservation

    api_request(
        admin_browser,
        "DELETE",
        f"/reservations/{reservation['id']}",
        allow_errors=True,
    )


@pytest.fixture
def transient_driver(admin_browser):
    """Upload a minimal driver package via API, yield its id, delete after test."""
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        driver_py = b"class Driver:\n    def status(self, *a, **k):\n        return {}\n"
        info = tarfile.TarInfo(name="driver.py")
        info.size = len(driver_py)
        tar.addfile(info, io.BytesIO(driver_py))
    buf.seek(0)

    files = {"file": ("e2e-driver.tar.gz", buf.getvalue(), "application/gzip")}
    data = {
        "name": f"e2e-driver-{int(time.time() * 1000)}",
        "description": "transient e2e driver",
        "connection_type": "Management",
    }
    resp = api_request(
        admin_browser,
        "POST",
        "/inventory/drivers",
        files=files,
        data=data,
        allow_errors=True,
    )
    if resp.status_code != 201:
        pytest.skip(f"driver upload failed: {resp.status_code} {resp.text}")
    driver_pkg = resp.json()

    yield driver_pkg

    api_request(
        admin_browser,
        "DELETE",
        f"/inventory/drivers/{driver_pkg['id']}",
        allow_errors=True,
    )
