"""Unit tests for e2e failure artifacts (durable evidence on disk for a gate failure).

Pure, stack-free tests: they exercise tests.e2e.conftest's FailureCapture,
artifact_dir_for, save_failure_artifacts, artifact_root, and
format_artifact_block directly, the pieces factored out of the
pytest_runtest_makereport hookwrapper and pytest_sessionfinish so they can be
pinned without a real Playwright/Selenium session. Mirrors the style of
tests/unit/test_e2e_seed_gate.py, which imports the same way from
tests.e2e.conftest.

Issue #1266 added the setup-phase branch of the hook, the per-test browser
registry, password redaction in page.html, the response-anchored pw_login,
the diagnosing _do_login timeout, and _open_selenium_driver's quit
finalizer. Those are pinned here with fake pages, drivers, responses, and
reports (the hook is driven as the generator it is), plus one inner pytest
run in a subprocess that exercises the real phases, the real module-scoped
fixture error cache, and the real finalizer order. No browser is started.
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest
from selenium.common.exceptions import NoSuchElementException, TimeoutException, WebDriverException

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
    """The slice of a Playwright Page the capture helpers touch."""

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


class FakeDriver:
    """The slice of a Selenium WebDriver the capture helpers touch."""

    def __init__(self, url="https://traefik/login", html=LOGIN_HTML):
        self.current_url = url
        self.page_source = html
        self.quit_calls = 0

    def get_screenshot_as_png(self):
        return b"PNG-driver"

    def get_log(self, kind):
        assert kind == "browser"
        return [{"level": "SEVERE", "message": "login 500"}]

    def implicitly_wait(self, seconds):
        self.implicit_wait = seconds

    def quit(self):
        self.quit_calls += 1


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
    assert report.sections == [
        (
            "e2e failure artifacts",
            "\n".join(
                [
                    str(out),
                    "traceback.txt",
                    "console.log",
                    "meta.txt",
                    "page.html",
                    "screenshot.png",
                ]
            ),
        )
    ]


def test_setup_failure_with_a_registered_driver_writes_five_files_under_a_setup_dir():
    e2e.register_setup_browser("selenium", FakeDriver())

    _run_hook(_report("setup"))

    out = _root() / SETUP_DIR
    assert _files(out) == FIVE_FILES
    assert (out / "screenshot.png").read_bytes() == b"PNG-driver"
    assert (out / "console.log").read_text() == ("{'level': 'SEVERE', 'message': 'login 500'}\n")
    assert "url: https://traefik/login" in (out / "meta.txt").read_text()


def test_setup_failure_with_nothing_registered_writes_the_text_files_and_says_so():
    _run_hook(_report("setup"))

    out = _root() / SETUP_DIR
    assert _files(out) == TEXT_FILES
    assert (out / "console.log").read_text() == ""
    meta = (out / "meta.txt").read_text().splitlines()
    assert meta[0] == "url: None"
    assert meta[-1] == "no page or driver was open when setup failed"


def test_setup_failure_falls_back_to_a_driver_the_resolved_fixtures_hold():
    # A module-scoped driver an EARLIER test created is not registered for
    # this test, but its setup resolved it before another fixture raised.
    _run_hook(_report("setup"), funcargs={"admin_browser": FakeDriver()})

    assert _files(_root() / SETUP_DIR) == FIVE_FILES


def test_setup_failure_with_two_registered_objects_writes_one_suffixed_dir_each():
    e2e.register_setup_browser("playwright", FakePage())
    e2e.register_setup_browser("selenium", FakeDriver())

    _run_hook(_report("setup"))

    assert _files(_root()) == {f"{SETUP_DIR}_page1_", f"{SETUP_DIR}_driver2_"}


def test_page_html_never_carries_the_typed_password():
    e2e.register_setup_browser("playwright", FakePage())
    e2e.register_setup_browser("selenium", FakeDriver())

    _run_hook(_report("setup"))

    for directory in _root().iterdir():
        html = (directory / "page.html").read_text()
        assert "hunter2" not in html
        assert 'type="password" value="[redacted]"' in html
        assert 'value="admin@example.com"' in html


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
    e2e.register_setup_browser("selenium", FakeDriver())

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
    e2e.register_setup_browser("selenium", FakeDriver())

    _run_hook(_report(when, failed=failed), funcargs={"pw_page": FakePage()})

    assert e2e._setup_browsers == []


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
    ],
)
def test_redact_password_values_leaves_everything_else_alone(html):
    assert e2e.redact_password_values(html) == html


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
        self.predicate = None

    def goto(self, url):
        self.events.append(("goto", url))

    def fill(self, selector, value):
        self.events.append(("fill", selector))

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


def test_pw_login_fails_at_once_with_the_status_and_body_on_a_non_200():
    page = LoginPage(FakeResponse(500, body="Internal Server Error"))

    with pytest.raises(AssertionError) as excinfo:
        e2e.pw_login(page, "admin@example.com", SECRET)

    assert str(excinfo.value) == (
        "POST /api/auth/login answered 500, expected 200: Internal Server Error"
    )
    assert not [e for e in page.events if e[0] == "wait_for_url"]


def test_pw_login_redacts_the_password_from_an_echoing_error_body():
    body = json.dumps({"detail": [{"input": {"email": "a@b.c", "password": SECRET}}]})
    assert 'hunter2\\"pw' in body
    page = LoginPage(FakeResponse(422, body=body))

    with pytest.raises(AssertionError) as excinfo:
        e2e.pw_login(page, "a@b.c", SECRET)

    message = str(excinfo.value)
    assert message.startswith("POST /api/auth/login answered 422, expected 200: ")
    assert "hunter2" not in message
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


class FakeElement:
    def __init__(self, driver, on_click=None):
        self._driver = driver
        self._on_click = on_click

    def clear(self):
        pass

    def send_keys(self, text):
        pass

    def click(self):
        if self._on_click:
            self._on_click()


class LoginDriver:
    """A fake WebDriver whose URL moves to /topology on submit only when told to."""

    def __init__(self, *, lands=True, statuses=None, form=True):
        self.current_url = "about:blank"
        self._lands = lands
        self._statuses = statuses if statuses is not None else []
        self._form = form

    def get(self, url):
        self.current_url = url

    def find_element(self, by, value):
        if not self._form:
            raise NoSuchElementException(value)
        if value == "button[type='submit']":
            return FakeElement(self, on_click=self._submit)
        return FakeElement(self)

    def _submit(self):
        if self._lands:
            self.current_url = "https://traefik/topology"

    def execute_script(self, script):
        assert script == e2e._LOGIN_STATUSES_JS
        if isinstance(self._statuses, Exception):
            raise self._statuses
        return self._statuses


@pytest.fixture
def no_wait(monkeypatch):
    # WAIT_TIMEOUT 0: WebDriverWait checks once and gives up, no sleep.
    monkeypatch.setattr(e2e, "WAIT_TIMEOUT", 0)


def test_do_login_returns_once_the_url_reaches_topology(no_wait):
    driver = LoginDriver(lands=True)

    e2e._do_login(driver, "https://traefik", "admin@example.com", SECRET)

    assert driver.current_url == "https://traefik/topology"


def test_do_login_timeout_names_the_url_and_the_login_statuses(no_wait):
    driver = LoginDriver(lands=False, statuses=[401])

    with pytest.raises(TimeoutException) as excinfo:
        e2e._do_login(driver, "https://traefik", "admin@example.com", SECRET)

    assert excinfo.value.msg == (
        "login did not reach /topology within 0s; the page is at https://traefik/login; "
        "POST /api/auth/login statuses the page received: [401]"
    )
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
    driver = LoginDriver(lands=False, statuses=statuses)

    with pytest.raises(TimeoutException) as excinfo:
        e2e._do_login(driver, "https://traefik", "admin@example.com", SECRET)

    assert excinfo.value.msg.endswith(f"POST /api/auth/login statuses the page received: {shown}")


def test_do_login_names_a_login_form_that_never_rendered(no_wait):
    with pytest.raises(TimeoutException) as excinfo:
        e2e._do_login(LoginDriver(form=False), "https://traefik", "admin@example.com", SECRET)

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
        driver = FakeDriver()
        created.append(driver)
        return driver

    monkeypatch.setattr(e2e, "_wait_for_selenium", lambda url: None)
    monkeypatch.setattr(e2e, "webdriver", SimpleNamespace(Remote=remote))
    return created


def test_a_login_that_raises_still_leaves_the_quit_registered(monkeypatch, fake_hub):
    request = FakeRequest()

    def login_times_out(driver, url, email, password):
        # The quit is registered BEFORE the login runs.
        assert request.finalizers == [driver.quit]
        raise TimeoutException("login timed out")

    monkeypatch.setattr(e2e, "_do_login", login_times_out)

    with pytest.raises(TimeoutException):
        e2e._open_selenium_driver(request, "https://traefik", ("admin@example.com", SECRET))

    (driver,) = fake_hub
    assert e2e._setup_browsers == [("selenium", driver)]
    assert driver.quit_calls == 0  # held open for the setup-failure capture
    for finalizer in request.finalizers:
        finalizer()
    assert driver.quit_calls == 1


def test_a_driver_without_credentials_never_logs_in(monkeypatch, fake_hub):
    def must_not_log_in(*args):
        raise AssertionError("logged in without credentials")

    monkeypatch.setattr(e2e, "_do_login", must_not_log_in)
    request = FakeRequest()

    driver = e2e._open_selenium_driver(request, "https://traefik")

    assert fake_hub == [driver]
    assert driver.implicit_wait == 5
    assert request.finalizers == [driver.quit]


# -- the hook and the fixtures inside a real pytest run --------------------------

_INNER_CONFTEST = """
import pytest
import tests.e2e.conftest as e2e
from tests.e2e.conftest import pytest_runtest_makereport  # noqa: F401  (the hook under test)

CREATED = []


class Driver:
    current_url = "https://traefik/login"
    page_source = "<html></html>"
    quits = 0

    def get_screenshot_as_png(self):
        return b"PNG"

    def get_log(self, kind):
        return []

    def implicitly_wait(self, seconds):
        pass

    def quit(self):
        Driver.quits += 1


def _remote(command_executor, options):
    CREATED.append(Driver())
    return CREATED[-1]


def _login_times_out(driver, url, email, password):
    raise TimeoutError("login timed out")


e2e._wait_for_selenium = lambda url: None
e2e.webdriver.Remote = _remote
e2e._do_login = _login_times_out


@pytest.fixture(autouse=True)
def _registry_starts_empty():
    assert e2e._setup_browsers == [], "a browser registered by an earlier test leaked"
"""

_INNER_TESTS = """
import pytest
import tests.e2e.conftest as e2e


class Page:
    url = "https://localhost/login"

    def content(self):
        return '<input type="password" value="hunter2">'

    def screenshot(self, full_page):
        return b"PNG"


@pytest.fixture
def pw_page():
    p = Page()
    e2e.register_setup_browser("playwright", p)
    return p


@pytest.fixture
def login_on_page_fails(pw_page):
    raise TimeoutError("login timed out")


@pytest.fixture
def fails_before_any_browser():
    raise RuntimeError("hub unreachable")


@pytest.fixture(scope="module")
def module_driver(request):
    return e2e._open_selenium_driver(request, "https://traefik", ("a@b.c", "pw"))


def test_passes(pw_page):
    pass


def test_setup_fails_on_a_page(login_on_page_fails):
    pass


def test_setup_fails_before_any_browser(fails_before_any_browser):
    pass


def test_call_fails(pw_page):
    assert False


def test_driver_first(module_driver):
    pass


def test_driver_second(module_driver):
    pass
"""

_INNER_AFTER = """
import tests.e2e.conftest as e2e
from conftest import CREATED, Driver


def test_after_the_module():
    assert len(CREATED) == 1, "the cached module fixture error opened a second session"
    assert Driver.quits == 1, "the driver whose login raised was never quit"
    assert e2e._setup_browsers == []
"""


def test_the_hook_and_the_selenium_fixture_in_a_real_pytest_run(tmp_path):
    (tmp_path / "conftest.py").write_text(textwrap.dedent(_INNER_CONFTEST))
    (tmp_path / "test_a_scenarios.py").write_text(textwrap.dedent(_INNER_TESTS))
    (tmp_path / "test_b_after.py").write_text(textwrap.dedent(_INNER_AFTER))
    root = tmp_path / "artifacts"
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT),
        "HERD_E2E_ARTIFACT_DIR": str(root),
    }

    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", str(tmp_path)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert run.stdout.splitlines()[-1].startswith("1 failed, 2 passed, 4 errors"), run.stdout
    prefix = "test_a_scenarios.py__"
    dirs = {p.name: _files(p) for p in root.iterdir()}
    assert dirs == {
        f"setup-{prefix}test_setup_fails_on_a_page": FIVE_FILES,
        f"setup-{prefix}test_setup_fails_before_any_browser": TEXT_FILES,
        f"{prefix}test_call_fails": FIVE_FILES,
        f"setup-{prefix}test_driver_first": FIVE_FILES,
        f"setup-{prefix}test_driver_second": TEXT_FILES,
    }
    page_html = (root / f"setup-{prefix}test_setup_fails_on_a_page" / "page.html").read_text()
    assert page_html == '<input type="password" value="[redacted]">'
