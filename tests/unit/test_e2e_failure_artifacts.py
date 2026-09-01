"""Unit tests for e2e failure artifacts (durable evidence on disk for a gate failure).

Pure, stack-free tests: they exercise tests.e2e.conftest's FailureCapture,
artifact_dir_for, save_failure_artifacts, artifact_root, and
format_artifact_block directly, the pieces factored out of the
pytest_runtest_makereport hookwrapper and pytest_sessionfinish so they can be
pinned without a real Playwright/Selenium session or a pytester run. Mirrors
the style of tests/unit/test_e2e_seed_gate.py, which imports the same way
from tests.e2e.conftest.
"""

from pathlib import Path

from tests.e2e.conftest import (
    FailureCapture,
    artifact_dir_for,
    artifact_root,
    format_artifact_block,
    save_failure_artifacts,
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
