"""Unit tests for e2e failure artifacts (durable evidence on disk for a gate failure).

Pure, stack-free tests: they exercise tests.e2e.conftest's FailureCapture,
artifact_dir_for, save_failure_artifacts, artifact_root, and
format_artifact_block directly, the pieces factored out of the
pytest_runtest_makereport hookwrapper and pytest_sessionfinish so they can be
pinned without a real Playwright/Selenium session. Mirrors the style of
tests/unit/test_e2e_seed_gate.py, which imports the same way from
tests.e2e.conftest.

Issue #1266 added the setup-phase branch of the hook, the per-test browser
registry, password redaction in page.html and in traceback arguments
(_Secret), the response-anchored pw_login, the diagnosing _do_login timeout,
the Selenium console log read through getLog, and _open_selenium_driver's quit
finalizer. Those are pinned here with fake pages, responses, and reports (the
hook is driven as the generator it is) and with WebDriver and WebElement mocks
autospecced from the real Selenium classes, so a fake cannot offer a method
the real remote driver lacks. One inner pytest run in a subprocess, under
`--tb=short` and again under `--tb=long -l`, exercises the real phases, the
real pw_page fixture's teardown order, the module-scoped fixture error cache,
and the finalizer order. No browser is started.
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest
from _pytest._io.saferepr import saferepr
from selenium.common.exceptions import NoSuchElementException, TimeoutException, WebDriverException
from selenium.webdriver.remote.command import Command
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement

import tests.e2e.conftest as e2e
from tests.e2e.conftest import (
    FailureCapture,
    artifact_dir_for,
    artifact_root,
    format_artifact_block,
    save_failure_artifacts,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIVE_FILES = {"screenshot.png", "page.html", "console.log", "traceback.txt", "meta.txt"}
TEXT_FILES = {"console.log", "traceback.txt", "meta.txt"}
NODEID = "tests/e2e/test_x.py::test_y"
SETUP_DIR = "setup-tests_e2e_test_x.py__test_y"
CALL_DIR = "tests_e2e_test_x.py__test_y"
SECRET = 'hunter2"pw'
# A non-ASCII password with a quote and a backslash, built with chr() so the
# source stays ASCII: FastAPI echoes it in a third JSON form (ensure_ascii=False).
NON_ASCII_SECRET = "p" + chr(0xE4) + '"ss\\w'
NO_BROWSER_NOTE_TEXT = (
    "no page or driver was registered or resolved for this test (none existed yet, "
    "or pytest re-raised a cached module-scoped fixture error without resolving "
    "the fixture again)"
)

# -- artifact_dir_for -----------------------------------------------------


def test_artifact_dir_for_sanitizes_separators_and_brackets(tmp_path):
    nodeid = "tests/e2e/test_x.py::test_y[param]"
    result = artifact_dir_for(nodeid, tmp_path)
    assert result == tmp_path / "tests_e2e_test_x.py__test_y_param_"


def test_artifact_dir_for_sanitizes_spaces():
    nodeid = "tests/e2e/test_a.py::test with spaces"
    result = artifact_dir_for(nodeid, Path("/root"))
    assert result == Path("/root/tests_e2e_test_a.py__test_with_spaces")


def test_artifact_dir_for_truncates_at_200_chars(tmp_path):
    nodeid = "tests/e2e/test_long.py::" + ("a" * 300)
    result = artifact_dir_for(nodeid, tmp_path)
    assert len(result.name) == 200


def test_artifact_dir_for_distinct_nodeids_never_collide(tmp_path):
    a = artifact_dir_for("tests/e2e/test_a.py::test_one", tmp_path)
    b = artifact_dir_for("tests/e2e/test_b.py::test_two", tmp_path)
    assert a != b


# -- save_failure_artifacts ------------------------------------------------


def test_save_failure_artifacts_writes_full_file_set(tmp_path):
    capture = FailureCapture(
        url="https://localhost/topology",
        html="<html><body>hi</body></html>",
        screenshot_png=b"\x89PNG\r\n\x1a\nfakebytes",
        console=["[log] hello", "[error] boom"],
        errors=[],
    )
    out_dir = save_failure_artifacts(
        tmp_path, "tests/e2e/test_x.py::test_y", capture, "Traceback: boom"
    )

    assert out_dir == tmp_path / "tests_e2e_test_x.py__test_y"
    assert (out_dir / "traceback.txt").read_text() == "Traceback: boom"
    assert (out_dir / "page.html").read_text() == "<html><body>hi</body></html>"
    assert (out_dir / "screenshot.png").read_bytes() == b"\x89PNG\r\n\x1a\nfakebytes"
    assert (out_dir / "console.log").read_text() == "[log] hello\n[error] boom\n"
    meta = (out_dir / "meta.txt").read_text()
    assert "url: https://localhost/topology" in meta
    assert "timestamp: " in meta


def test_save_failure_artifacts_omits_html_and_screenshot_when_none(tmp_path):
    capture = FailureCapture(url="https://localhost/x", html=None, screenshot_png=None)
    out_dir = save_failure_artifacts(tmp_path, "tests/e2e/test_x.py::test_z", capture, "tb")

    assert not (out_dir / "page.html").exists()
    assert not (out_dir / "screenshot.png").exists()
    assert (out_dir / "console.log").exists()
    assert (out_dir / "meta.txt").exists()
    assert (out_dir / "traceback.txt").exists()


def test_save_failure_artifacts_console_log_empty_file_when_no_messages(tmp_path):
    capture = FailureCapture(console=[])
    out_dir = save_failure_artifacts(tmp_path, "tests/e2e/test_x.py::test_empty", capture, "tb")
    assert (out_dir / "console.log").read_text() == ""


def test_save_failure_artifacts_meta_contains_url_and_capture_errors_verbatim(tmp_path):
    capture = FailureCapture(
        url="https://localhost/topology",
        errors=["screenshot: TimeoutError: 30000ms exceeded", "content: RuntimeError: closed"],
    )
    out_dir = save_failure_artifacts(tmp_path, "tests/e2e/test_x.py::test_err", capture, "tb")
    meta = (out_dir / "meta.txt").read_text()
    assert "url: https://localhost/topology" in meta
    assert "screenshot: TimeoutError: 30000ms exceeded" in meta
    assert "content: RuntimeError: closed" in meta


def test_save_failure_artifacts_traceback_byte_equals_longrepr(tmp_path):
    longrepr = "AssertionError: probe failure\n  assert False"
    out_dir = save_failure_artifacts(
        tmp_path, "tests/e2e/test_x.py::test_tb", FailureCapture(), longrepr
    )
    assert (out_dir / "traceback.txt").read_bytes() == longrepr.encode()


def test_save_failure_artifacts_rerun_overwrites(tmp_path):
    nodeid = "tests/e2e/test_x.py::test_rerun"
    first = save_failure_artifacts(tmp_path, nodeid, FailureCapture(), "first failure")
    second = save_failure_artifacts(tmp_path, nodeid, FailureCapture(), "second failure")

    assert first == second
    assert (second / "traceback.txt").read_text() == "second failure"


# -- artifact_root ----------------------------------------------------------


def test_artifact_root_honors_env_override(monkeypatch, tmp_path):
    override = tmp_path / "custom-artifacts"
    monkeypatch.setenv("HERD_E2E_ARTIFACT_DIR", str(override))
    assert artifact_root() == override


def test_artifact_root_falls_back_to_tempdir_default(monkeypatch):
    import tempfile

    monkeypatch.delenv("HERD_E2E_ARTIFACT_DIR", raising=False)
    assert artifact_root() == Path(tempfile.gettempdir()) / "herd-e2e-artifacts"


# -- format_artifact_block ---------------------------------------------------


def test_format_artifact_block_header_and_one_line_per_dir():
    dirs = [Path("/tmp/herd-e2e-artifacts/test_a"), Path("/tmp/herd-e2e-artifacts/test_b")]
    block = format_artifact_block(dirs)
    lines = block.splitlines()
    assert lines[0] == "e2e failure artifacts written for 2 test(s):"
    assert lines[1] == "  /tmp/herd-e2e-artifacts/test_a"
    assert lines[2] == "  /tmp/herd-e2e-artifacts/test_b"


def test_format_artifact_block_single_dir():
    block = format_artifact_block([Path("/tmp/herd-e2e-artifacts/only")])
    assert block == ("e2e failure artifacts written for 1 test(s):\n  /tmp/herd-e2e-artifacts/only")


def test_save_failure_artifacts_round_trips_non_ascii_as_utf8(tmp_path):
    capture = FailureCapture(html="<p>café 中文</p>", console=["[log] über"])
    out_dir = save_failure_artifacts(
        tmp_path, "tests/e2e/test_x.py::test_utf8", capture, "AssertionError: ☃"
    )
    assert (out_dir / "page.html").read_bytes() == "<p>café 中文</p>".encode("utf-8")
    assert (out_dir / "console.log").read_bytes() == "[log] über\n".encode("utf-8")
    assert (out_dir / "traceback.txt").read_bytes() == "AssertionError: ☃".encode("utf-8")


# -- shared fakes for the issue #1266 tests ----------------------------------


@pytest.fixture(autouse=True)
def _isolated_hook_state(monkeypatch, tmp_path):
    """Empty registry and written-dir list per test, artifacts under tmp_path."""
    monkeypatch.setenv("HERD_E2E_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    e2e._setup_browsers.clear()
    e2e._artifact_dirs_written.clear()
    yield
    e2e._setup_browsers.clear()
    e2e._artifact_dirs_written.clear()


LOGIN_HTML = (
    '<form><input id="login-email" type="email" value="admin@example.com">'
    '<input id="login-password" type="password" value="hunter2&quot;pw"></form>'
)


class FakePage:
    """The slice of a Playwright Page the capture helpers and pw_page touch."""

    def __init__(self, url="https://localhost/login", html=LOGIN_HTML, screenshot=b"PNG-page"):
        self.url = url
        self._html = html
        self._screenshot = screenshot

    def content(self):
        return self._html

    def screenshot(self, full_page):
        assert full_page is True
        if isinstance(self._screenshot, Exception):
            raise self._screenshot
        return self._screenshot

    def on(self, event, callback):
        pass


def _spec_driver(url="https://traefik/login", html=LOGIN_HTML):
    """A WebDriver mock autospecced from the real remote class (spec_set: no invented methods)."""
    driver = create_autospec(WebDriver, instance=True, spec_set=True)
    driver.current_url = url
    driver.page_source = html
    driver.get_screenshot_as_png.return_value = b"PNG-driver"
    driver.execute.return_value = {"value": [{"level": "SEVERE", "message": "login 500"}]}
    return driver


def _report(when, failed=True, nodeid=NODEID, longrepr="TimeoutError: login timed out"):
    return SimpleNamespace(when=when, failed=failed, nodeid=nodeid, longrepr=longrepr, sections=[])


def _run_hook(report, funcargs=None):
    """Drive pytest_runtest_makereport the way pluggy drives a wrapper=True hook."""
    item = SimpleNamespace(funcargs=funcargs or {})
    gen = e2e.pytest_runtest_makereport(item, None)
    next(gen)
    with pytest.raises(StopIteration) as stop:
        gen.send(report)
    return stop.value.value


def _root() -> Path:
    return artifact_root()


def _files(directory: Path) -> set[str]:
    return {p.name for p in directory.iterdir()}


# -- setup-phase artifacts (issue #1266) --------------------------------------


def test_setup_failure_with_a_registered_page_writes_five_files_under_a_setup_dir(monkeypatch):
    page = FakePage()
    e2e.register_setup_browser("playwright", page)
    monkeypatch.setitem(e2e._pw_console_log, id(page), ["[error] POST /api/auth/login 500"])

    report = _run_hook(_report("setup"))

    out = _root() / SETUP_DIR
    assert _files(_root()) == {SETUP_DIR}
    assert _files(out) == FIVE_FILES
    assert (out / "screenshot.png").read_bytes() == b"PNG-page"
    assert (out / "console.log").read_text() == "[error] POST /api/auth/login 500\n"
    assert (out / "traceback.txt").read_text() == "TimeoutError: login timed out"
    assert (out / "meta.txt").read_text().startswith("url: https://localhost/login\n")
    listing = [str(out), "traceback.txt", "console.log", "meta.txt", "page.html", "screenshot.png"]
    assert report.sections == [("e2e failure artifacts", "\n".join(listing))]


def test_setup_failure_with_a_registered_driver_writes_its_browser_log_through_getlog():
    driver = _spec_driver()
    e2e.register_setup_browser("selenium", driver)

    _run_hook(_report("setup"))

    out = _root() / SETUP_DIR
    assert _files(out) == FIVE_FILES
    assert (out / "screenshot.png").read_bytes() == b"PNG-driver"
    assert (out / "console.log").read_text() == "{'level': 'SEVERE', 'message': 'login 500'}\n"
    driver.execute.assert_called_once_with(Command.GET_LOG, {"type": "browser"})
    assert "url: https://traefik/login" in (out / "meta.txt").read_text()


def test_a_browser_log_the_grid_refuses_is_named_in_meta_not_swallowed():
    driver = _spec_driver()
    driver.execute.side_effect = WebDriverException("unknown command: getLog")
    e2e.register_setup_browser("selenium", driver)

    _run_hook(_report("setup"))

    out = _root() / SETUP_DIR
    assert (out / "console.log").read_text() == ""
    assert "console: Message: unknown command: getLog" in (out / "meta.txt").read_text()


def test_chrome_sessions_ask_for_the_browser_log():
    capabilities = e2e._make_chrome_options().to_capabilities()
    assert capabilities["goog:loggingPrefs"] == {"browser": "ALL"}


def test_setup_failure_with_nothing_registered_writes_the_text_files_and_says_so():
    _run_hook(_report("setup"))

    out = _root() / SETUP_DIR
    assert _files(out) == TEXT_FILES
    assert (out / "console.log").read_text() == ""
    meta = (out / "meta.txt").read_text().splitlines()
    assert meta[0] == "url: None"
    assert meta[-1] == NO_BROWSER_NOTE_TEXT


def test_setup_failure_falls_back_to_a_driver_the_resolved_fixtures_hold():
    # A module-scoped driver an EARLIER test created is not registered for
    # this test, but its setup resolved it before another fixture raised.
    _run_hook(_report("setup"), funcargs={"admin_browser": _spec_driver()})

    assert _files(_root() / SETUP_DIR) == FIVE_FILES


def test_two_registered_objects_get_one_suffixed_dir_each_and_no_typed_password():
    e2e.register_setup_browser("playwright", FakePage())
    e2e.register_setup_browser("selenium", _spec_driver())

    _run_hook(_report("setup"))

    assert _files(_root()) == {f"{SETUP_DIR}_page1_", f"{SETUP_DIR}_driver2_"}
    for directory in _root().iterdir():
        html = (directory / "page.html").read_text()
        assert "hunter2" not in html
        assert 'type="password" value="[redacted]"' in html
        assert 'value="admin@example.com"' in html


def test_pw_page_registers_its_page_for_a_setup_failure():
    context = SimpleNamespace(page=FakePage(), closed=False)
    context.new_page = lambda: context.page
    context.close = lambda: setattr(context, "closed", True)
    pw_browser = SimpleNamespace(new_context=lambda **kwargs: context)

    fixture = e2e.pw_page.__wrapped__(pw_browser)
    page = next(fixture)

    assert e2e._setup_browsers == [("playwright", page)]
    with pytest.raises(StopIteration):
        next(fixture)
    assert context.closed is True


def test_a_screenshot_that_raises_does_not_hide_the_original_failure():
    e2e.register_setup_browser("playwright", FakePage(screenshot=RuntimeError("renderer gone")))
    original = _report("setup")

    report = _run_hook(original)

    assert report is original
    assert report.failed is True
    assert report.longrepr == "TimeoutError: login timed out"
    out = _root() / SETUP_DIR
    assert _files(out) == FIVE_FILES - {"screenshot.png"}
    assert "screenshot: renderer gone" in (out / "meta.txt").read_text().splitlines()
    assert (out / "traceback.txt").read_text() == "TimeoutError: login timed out"


def test_a_write_that_raises_is_reported_in_the_sections_not_raised(monkeypatch):
    def disk_full(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(e2e, "save_failure_artifacts", disk_full)
    e2e.register_setup_browser("playwright", FakePage())
    original = _report("setup")

    report = _run_hook(original)

    assert report is original
    assert report.sections == [("e2e failure artifacts", "failed to write artifacts: disk full")]
    assert e2e._setup_browsers == []


def test_a_call_failure_still_uses_the_test_fixtures_and_the_unprefixed_dir():
    # A stale registry entry must not change what a CALL failure captures.
    e2e.register_setup_browser("selenium", _spec_driver())

    _run_hook(_report("call"), funcargs={"pw_page": FakePage()})

    assert _files(_root()) == {CALL_DIR}
    assert (_root() / CALL_DIR / "screenshot.png").read_bytes() == b"PNG-page"


def test_a_call_failure_with_no_browser_fixture_still_writes_nothing():
    _run_hook(_report("call"))

    assert not _root().exists()


@pytest.mark.parametrize(
    ("when", "failed"),
    [
        ("setup", False),
        ("call", False),
        ("teardown", False),
        ("teardown", True),
    ],
)
def test_passing_phases_and_teardown_failures_write_nothing(when, failed):
    e2e.register_setup_browser("playwright", FakePage())

    report = _run_hook(_report(when, failed=failed))

    assert not _root().exists()
    assert report.sections == []


@pytest.mark.parametrize(
    ("when", "failed"),
    [
        ("setup", False),
        ("setup", True),
        ("call", False),
        ("call", True),
        ("teardown", False),
        ("teardown", True),
    ],
)
def test_the_registry_is_empty_after_every_phase_whatever_its_outcome(when, failed):
    e2e.register_setup_browser("playwright", FakePage())
    e2e.register_setup_browser("selenium", _spec_driver())

    _run_hook(_report(when, failed=failed), funcargs={"pw_page": FakePage()})

    assert e2e._setup_browsers == []


# -- password redaction -------------------------------------------------------


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        (
            '<input id="p" type="password" value="s3cret">',
            '<input id="p" type="password" value="[redacted]">',
        ),
        (
            '<input value="s3cret" class="x" type="password">',
            '<input value="[redacted]" class="x" type="password">',
        ),
        (
            "<INPUT TYPE='Password' VALUE='s3cret'>",
            "<INPUT TYPE='Password' VALUE=\"[redacted]\">",
        ),
        (
            "<input type=password value=s3cret>",
            '<input type=password value="[redacted]">',
        ),
        (
            '<input type="password" value="a>b&quot;c" id="p">',
            '<input type="password" value="[redacted]" id="p">',
        ),
        (
            '<input type="password" title="x value=" value="secret">',
            '<input type="password" title="x value=" value="[redacted]">',
        ),
    ],
)
def test_redact_password_values_replaces_the_value_of_a_password_input(html, expected):
    assert e2e.redact_password_values(html) == expected


@pytest.mark.parametrize(
    "html",
    [
        '<input id="e" type="email" value="admin@example.com">',
        '<input id="p" type="password">',
        '<p>type="password" value="not an input"</p>',
        '<input title="type=password" value="kept">',
        '<input type="text" data-x=" type=&quot;password&quot;" value="kept">',
    ],
)
def test_redact_password_values_leaves_everything_else_alone(html):
    assert e2e.redact_password_values(html) == html


# -- _Secret: no password in a traceback argument or local ----------------------


def test_the_admin_password_prints_redacted_and_types_as_itself():
    secret = e2e._Secret('p"w')
    assert repr(e2e.ADMIN_PASSWORD) == "'[redacted]'"
    assert saferepr(secret) == "'[redacted]'"
    assert saferepr(("a@b.c", secret)) == "('a@b.c', '[redacted]')"
    assert str(secret) == 'p"w' and type(str(secret)) is str
    assert f"{secret}" == 'p"w'
    assert json.dumps({"password": secret}) == '{"password": "p\\"w"}'


# -- pw_login (issue #1266) -----------------------------------------------------


class FakeResponse:
    def __init__(self, status, body="", method="POST", url="https://localhost/api/auth/login"):
        self.status = status
        self.url = url
        self.request = SimpleNamespace(method=method)
        self._body = body
        self.text_reads = 0

    def text(self):
        self.text_reads += 1
        return self._body


class LoginPage:
    """A fake Page recording the order of the calls pw_login makes."""

    def __init__(self, response):
        self.response = response
        self.events = []
        self.typed = {}
        self.predicate = None

    def goto(self, url):
        self.events.append(("goto", url))

    def fill(self, selector, value):
        self.events.append(("fill", selector))
        self.typed[selector] = value

    def click(self, selector):
        self.events.append(("click", selector))

    def expect_response(self, predicate):
        self.predicate = predicate
        page = self

        class _Waiter:
            def __enter__(self):
                page.events.append(("listen",))
                return SimpleNamespace(value=page.response)

            def __exit__(self, *exc):
                page.events.append(("answered",))
                return False

        return _Waiter()

    def wait_for_url(self, pattern, **kwargs):
        self.events.append(("wait_for_url", pattern, kwargs))


def test_pw_login_clicks_inside_the_login_response_wait_then_waits_for_commit():
    page = LoginPage(FakeResponse(200, body='{"access_token": "t"}'))

    e2e.pw_login(page, "admin@example.com", SECRET)

    assert page.events == [
        ("goto", f"{e2e.HOST_BASE_URL}/login"),
        ("fill", "#login-email"),
        ("fill", "#login-password"),
        ("listen",),
        ("click", "button[type='submit']"),
        ("answered",),
        ("wait_for_url", "**/topology**", {"wait_until": "commit"}),
    ]
    assert page.predicate is e2e.pw_is_login_post
    # A 200's body carries the tokens and is never read.
    assert page.response.text_reads == 0
    # A plain str password is typed as itself and prints redacted.
    assert page.typed["#login-password"] == SECRET
    assert repr(page.typed["#login-password"]) == "'[redacted]'"


def test_pw_login_fails_at_once_with_the_status_and_body_on_a_non_200():
    page = LoginPage(FakeResponse(500, body="Internal Server Error"))

    with pytest.raises(AssertionError) as excinfo:
        e2e.pw_login(page, "admin@example.com", SECRET)

    assert str(excinfo.value) == (
        "POST /api/auth/login answered 500, expected 200: Internal Server Error"
    )
    assert not [e for e in page.events if e[0] == "wait_for_url"]


@pytest.mark.parametrize(
    ("password", "ensure_ascii"),
    [(SECRET, True), (NON_ASCII_SECRET, False), (NON_ASCII_SECRET, True)],
    ids=["ascii-quote", "non-ascii-as-fastapi-serializes", "non-ascii-ascii-escaped"],
)
def test_pw_login_redacts_the_password_from_an_echoing_error_body(password, ensure_ascii):
    body = json.dumps(
        {"detail": [{"input": {"email": "a@b.c", "password": password}}]},
        ensure_ascii=ensure_ascii,
    )
    assert password not in body  # only an escaped form is echoed
    page = LoginPage(FakeResponse(422, body=body))

    with pytest.raises(AssertionError) as excinfo:
        e2e.pw_login(page, "a@b.c", password)

    message = str(excinfo.value)
    assert message.startswith("POST /api/auth/login answered 422, expected 200: ")
    assert password[:2] not in message
    assert '"password": "[redacted]"' in message


def test_pw_login_failure_raises_from_a_frame_that_holds_no_password():
    # pytest --tb=auto prints the LAST frame's arguments; it must not be pw_login's.
    page = LoginPage(FakeResponse(401, body='{"detail": "Invalid credentials"}'))

    with pytest.raises(AssertionError) as excinfo:
        e2e.pw_login(page, "admin@example.com", SECRET)

    last = excinfo.tb
    while last.tb_next is not None:
        last = last.tb_next
    assert last.tb_frame.f_code.co_name == "_fail_login"
    assert SECRET not in repr(last.tb_frame.f_locals)


@pytest.mark.parametrize(
    ("method", "url", "expected"),
    [
        ("POST", "https://localhost/api/auth/login", True),
        ("POST", "https://192.168.1.27/api/auth/login?next=x", True),
        ("GET", "https://localhost/api/auth/login", False),
        ("POST", "https://localhost/api/auth/login/extra", False),
        ("POST", "https://localhost/api/auth/register", False),
    ],
)
def test_pw_is_login_post_matches_only_the_login_post(method, url, expected):
    assert e2e.pw_is_login_post(FakeResponse(200, method=method, url=url)) is expected


# -- _do_login (issue #1266) ----------------------------------------------------


def _login_driver(*, lands=True, statuses=(), form=True):
    """An autospecced WebDriver whose URL moves to /topology on submit only when `lands`.

    Returns the driver and a dict of the elements it handed out, keyed by locator.
    """
    driver = create_autospec(WebDriver, instance=True, spec_set=True)
    driver.current_url = "about:blank"
    elements = {}

    def get(url):
        driver.current_url = url

    def submit():
        if lands:
            driver.current_url = "https://traefik/topology"

    def find_element(by, value):
        if not form:
            raise NoSuchElementException(value)
        element = elements.setdefault(value, create_autospec(WebElement, instance=True))
        if value == "button[type='submit']":
            element.click.side_effect = submit
        return element

    driver.get.side_effect = get
    driver.find_element.side_effect = find_element
    if isinstance(statuses, Exception):
        driver.execute_script.side_effect = statuses
    else:
        driver.execute_script.return_value = list(statuses)
    return driver, elements


@pytest.fixture
def no_wait(monkeypatch):
    # WAIT_TIMEOUT 0: WebDriverWait checks once and gives up, no sleep.
    monkeypatch.setattr(e2e, "WAIT_TIMEOUT", 0)


def test_do_login_types_the_password_and_returns_once_the_url_reaches_topology(no_wait):
    driver, elements = _login_driver(lands=True)

    e2e._do_login(driver, "https://traefik", "admin@example.com", SECRET)

    assert driver.current_url == "https://traefik/topology"
    (typed,) = elements["login-password"].send_keys.call_args.args
    assert typed == SECRET and repr(typed) == "'[redacted]'"


def test_do_login_timeout_names_the_url_and_the_login_statuses(no_wait):
    driver, _ = _login_driver(lands=False, statuses=[401])

    with pytest.raises(TimeoutException) as excinfo:
        e2e._do_login(driver, "https://traefik", "admin@example.com", SECRET)

    assert excinfo.value.msg == (
        "login did not reach /topology within 0s; the page is at https://traefik/login; "
        "POST /api/auth/login statuses the page received: [401]"
    )
    driver.execute_script.assert_called_once_with(e2e._LOGIN_STATUSES_JS)
    # Raised outside the except block: no chained exception repeats _do_login's frame.
    assert excinfo.value.__context__ is None
    assert excinfo.value.__cause__ is None
    last = excinfo.tb
    while last.tb_next is not None:
        last = last.tb_next
    assert last.tb_frame.f_code.co_name == "_raise_selenium_login_timeout"
    assert SECRET not in repr(last.tb_frame.f_locals)


@pytest.mark.parametrize(
    ("statuses", "shown"),
    [([], "none"), (WebDriverException("gone"), "(unavailable: WebDriverException)")],
)
def test_do_login_timeout_says_when_no_login_status_is_known(no_wait, statuses, shown):
    driver, _ = _login_driver(lands=False, statuses=statuses)

    with pytest.raises(TimeoutException) as excinfo:
        e2e._do_login(driver, "https://traefik", "admin@example.com", SECRET)

    assert excinfo.value.msg.endswith(f"POST /api/auth/login statuses the page received: {shown}")


def test_do_login_names_a_login_form_that_never_rendered(no_wait):
    driver, _ = _login_driver(form=False)

    with pytest.raises(TimeoutException) as excinfo:
        e2e._do_login(driver, "https://traefik", "admin@example.com", SECRET)

    assert excinfo.value.msg == "the login form (#login-email) did not render"


# -- _open_selenium_driver (issue #1266, the leaked session) --------------------


class FakeRequest:
    def __init__(self):
        self.finalizers = []

    def addfinalizer(self, fn):
        self.finalizers.append(fn)


@pytest.fixture
def fake_hub(monkeypatch):
    """Patch the hub wait and webdriver.Remote; return the drivers created."""
    created = []

    def remote(command_executor, options):
        driver = _spec_driver()
        created.append(driver)
        return driver

    monkeypatch.setattr(e2e, "_wait_for_selenium", lambda url: None)
    monkeypatch.setattr(e2e, "webdriver", SimpleNamespace(Remote=remote))
    return created


def test_a_login_that_raises_still_leaves_the_quit_registered(monkeypatch, fake_hub):
    request = FakeRequest()
    seen = {}

    def login_times_out(driver, url, email, password):
        # The quit is registered BEFORE the login runs.
        assert request.finalizers == [driver.quit]
        seen["password"] = password
        raise TimeoutException("login timed out")

    monkeypatch.setattr(e2e, "_do_login", login_times_out)

    with pytest.raises(TimeoutException):
        e2e._open_selenium_driver(request, "https://traefik", ("admin@example.com", SECRET))

    (driver,) = fake_hub
    assert seen["password"] == SECRET and repr(seen["password"]) == "'[redacted]'"
    assert e2e._setup_browsers == [("selenium", driver)]
    driver.quit.assert_not_called()  # held open for the setup-failure capture
    for finalizer in request.finalizers:
        finalizer()
    driver.quit.assert_called_once_with()


def test_a_driver_without_credentials_never_logs_in(monkeypatch, fake_hub):
    def must_not_log_in(*args):
        raise AssertionError("logged in without credentials")

    monkeypatch.setattr(e2e, "_do_login", must_not_log_in)
    request = FakeRequest()

    driver = e2e._open_selenium_driver(request, "https://traefik")

    assert fake_hub == [driver]
    driver.implicitly_wait.assert_called_once_with(5)
    assert request.finalizers == [driver.quit]


# -- the hook and the fixtures inside a real pytest run --------------------------

_INNER_CONFTEST = """
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement

import tests.e2e.conftest as e2e
from tests.e2e.conftest import pw_page  # noqa: F401  (the real fixture)
from tests.e2e.conftest import pytest_runtest_makereport  # noqa: F401  (the hook under test)

SECRET = "inner-run-s3cret"
CREATED = []


class Page:
    # A Playwright Page stand-in: once its context is closed every read raises,
    # so a capture that ran after pw_page's teardown would write no page.html.

    def __init__(self):
        self.url = "https://localhost/login"
        self.closed = False
        self.login_status = 200
        self.login_body = ""
        self.typed = {}

    def on(self, event, callback):
        pass

    def goto(self, url):
        self.url = url

    def fill(self, selector, value):
        self.typed[selector] = value

    def click(self, selector):
        pass

    def expect_response(self, predicate):
        page = self
        response = SimpleNamespace(
            status=page.login_status,
            url="https://localhost/api/auth/login",
            request=SimpleNamespace(method="POST"),
            text=lambda: page.login_body,
        )
        assert predicate(response)

        class _Waiter:
            def __enter__(self):
                return SimpleNamespace(value=response)

            def __exit__(self, *exc):
                return False

        return _Waiter()

    def wait_for_url(self, pattern, wait_until):
        self.url = "https://localhost/topology"

    def content(self):
        if self.closed:
            raise RuntimeError("Target page, context or browser has been closed")
        return '<input type="password" value="%s">' % self.typed.get("#login-password", "")

    def screenshot(self, full_page):
        if self.closed:
            raise RuntimeError("Target page, context or browser has been closed")
        return b"PNG"


class Context:
    def __init__(self):
        self.page = Page()

    def new_page(self):
        return self.page

    def close(self):
        self.page.closed = True


@pytest.fixture
def pw_browser():
    return SimpleNamespace(new_context=lambda **kwargs: Context())


def _remote(command_executor, options):
    assert options.to_capabilities()["goog:loggingPrefs"] == {"browser": "ALL"}
    driver = create_autospec(WebDriver, instance=True, spec_set=True)
    driver.current_url = "https://traefik/login"
    driver.page_source = "<html></html>"
    driver.get_screenshot_as_png.return_value = b"PNG"
    driver.execute.return_value = {"value": [{"level": "SEVERE", "message": "login 401"}]}
    driver.execute_script.return_value = [401]
    driver.find_element.return_value = create_autospec(WebElement, instance=True)
    CREATED.append(driver)
    return driver


e2e._wait_for_selenium = lambda url: None
e2e.webdriver.Remote = _remote
e2e.WAIT_TIMEOUT = 0


@pytest.fixture(autouse=True)
def _registry_starts_empty():
    assert e2e._setup_browsers == [], "a browser registered by an earlier test leaked"
"""

_INNER_TESTS = """
import pytest
import tests.e2e.conftest as e2e
from conftest import SECRET


@pytest.fixture
def login_answers_500(pw_page):
    pw_page.login_status = 500
    pw_page.login_body = '{"detail": "boom", "input": "' + SECRET + '"}'
    e2e.pw_login(pw_page, "a@b.c", SECRET)


@pytest.fixture
def fails_before_any_browser():
    raise RuntimeError("hub unreachable")


@pytest.fixture(scope="module")
def module_driver(request):
    return e2e._open_selenium_driver(request, "https://traefik", ("a@b.c", SECRET))


def test_passes(pw_page):
    e2e.pw_login(pw_page, "a@b.c", SECRET)


def test_setup_login_fails_on_a_page(login_answers_500):
    pass


def test_setup_fails_before_any_browser(fails_before_any_browser):
    pass


def test_call_fails(pw_page):
    assert False


def test_call_login_fails(pw_page):
    pw_page.login_status = 401
    e2e.pw_login(pw_page, "a@b.c", SECRET)


def test_driver_first(module_driver):
    pass


def test_driver_second(module_driver):
    pass
"""

_INNER_AFTER = """
import tests.e2e.conftest as e2e
from conftest import CREATED


def test_after_the_module():
    assert len(CREATED) == 1, "the cached module fixture error opened a second session"
    CREATED[0].quit.assert_called_once_with()
    assert e2e._setup_browsers == []
"""

INNER_SECRET = "inner-run-s3cret"


@pytest.mark.parametrize(
    "tb_args", [["--tb=short"], ["--tb=long", "-l"]], ids=["tb-short", "tb-long-with-locals"]
)
def test_the_hook_and_the_fixtures_in_a_real_pytest_run(tmp_path, tb_args):
    (tmp_path / "conftest.py").write_text(textwrap.dedent(_INNER_CONFTEST))
    (tmp_path / "test_a_scenarios.py").write_text(textwrap.dedent(_INNER_TESTS))
    (tmp_path / "test_b_after.py").write_text(textwrap.dedent(_INNER_AFTER))
    root = tmp_path / "artifacts"
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), "HERD_E2E_ARTIFACT_DIR": str(root)}

    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-q",
            "-W",
            "error",
            *tb_args,
            str(tmp_path),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert run.stdout.splitlines()[-1].startswith("2 failed, 2 passed, 4 errors"), run.stdout
    p = "test_a_scenarios.py__"
    dirs = {d.name: _files(d) for d in root.iterdir()}
    assert dirs == {
        f"setup-{p}test_setup_login_fails_on_a_page": FIVE_FILES,
        f"setup-{p}test_setup_fails_before_any_browser": TEXT_FILES,
        f"{p}test_call_fails": FIVE_FILES,
        f"{p}test_call_login_fails": FIVE_FILES,
        f"setup-{p}test_driver_first": FIVE_FILES,
        f"setup-{p}test_driver_second": TEXT_FILES,
    }
    page_dir = root / f"setup-{p}test_setup_login_fails_on_a_page"
    # The real pw_page's teardown ran after the hook: the page could still be read.
    assert (page_dir / "page.html").read_text() == '<input type="password" value="[redacted]">'
    assert (
        "POST /api/auth/login answered 500, expected 200: "
        in (page_dir / "traceback.txt").read_text()
    )
    driver_dir = root / f"setup-{p}test_driver_first"
    assert "login 401" in (driver_dir / "console.log").read_text()
    assert (
        "POST /api/auth/login statuses the page received: [401]"
        in (driver_dir / "traceback.txt").read_text()
    )
    second_meta = (root / f"setup-{p}test_driver_second" / "meta.txt").read_text()
    assert second_meta.splitlines()[-1] == NO_BROWSER_NOTE_TEXT

    # No form of the password anywhere: stdout, stderr, or any artifact file.
    assert INNER_SECRET not in run.stdout + run.stderr
    leaks = [f for f in root.rglob("*") if f.is_file() and INNER_SECRET.encode() in f.read_bytes()]
    assert leaks == []
    if "--tb=long" in tb_args:
        # Not vacuous: the frames' arguments WERE printed, redacted.
        for directory in (page_dir, driver_dir):
            assert "password = '[redacted]'" in (directory / "traceback.txt").read_text()
