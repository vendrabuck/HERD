"""Unit tests for scripts/check_image_matches_lock.py (issue #593).

The script lives at the repo root, not inside a package, so it is loaded by
path the same way tests/unit/test_seed_build_canvas.py loads
seed_devices_public.py. No image build or stack is needed: `load_pins` reads
plain requirements-style text files that the tests write to `tmp_path`, and
`main` is driven end to end through `sys.argv` with those files plus captured
stdout, pinning the exact drift messages an image-vs-lock mismatch prints.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "check_image_matches_lock.py"


@pytest.fixture(scope="module")
def script():
    spec = importlib.util.spec_from_file_location("check_image_matches_lock", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_image_matches_lock"] = module
    spec.loader.exec_module(module)
    return module


# --- normalize_name: PEP 503 equivalence ------------------------------------


def test_normalize_name_folds_case(script):
    assert script.normalize_name("Foo_Bar") == script.normalize_name("foo-bar")


def test_normalize_name_treats_dot_underscore_dash_as_equivalent(script):
    assert script.normalize_name("pdfminer.six") == script.normalize_name("pdfminer-six")
    assert script.normalize_name("pdfminer_six") == script.normalize_name("pdfminer-six")


def test_normalize_name_collapses_runs_of_separators(script):
    # PEP 503 treats any run of -, _, . as a single separator.
    assert script.normalize_name("foo__bar..baz") == "foo-bar-baz"


def test_normalize_name_exact_value(script):
    assert script.normalize_name("Foo_Bar") == "foo-bar"


# --- parse_requirement_line ---------------------------------------------


def test_parse_requirement_line_simple_pin(script):
    assert script.parse_requirement_line("httpx==0.28.1") == ("httpx", "0.28.1", False)


def test_parse_requirement_line_with_marker(script):
    parsed = script.parse_requirement_line("colorama==0.4.6 ; sys_platform == 'win32'")
    assert parsed == ("colorama", "0.4.6", True)


def test_parse_requirement_line_normalizes_name(script):
    name, version, has_marker = script.parse_requirement_line("Foo_Bar==1.2.3")
    assert name == "foo-bar"
    assert version == "1.2.3"
    assert has_marker is False


def test_parse_requirement_line_blank_returns_none(script):
    assert script.parse_requirement_line("") is None
    assert script.parse_requirement_line("   ") is None


def test_parse_requirement_line_comment_returns_none(script):
    assert script.parse_requirement_line("# this is a comment") is None


def test_parse_requirement_line_no_pin_returns_none(script):
    # A line with no "==" (e.g. a bare package name or a VCS url) is not a pin.
    assert script.parse_requirement_line("some-package") is None


# --- load_pins ---------------------------------------------------------


def test_load_pins_returns_names_versions_and_markered_set(script, tmp_path):
    reqs = tmp_path / "reqs.txt"
    reqs.write_text(
        "httpx==0.28.1\n# a comment\n\ncolorama==0.4.6 ; sys_platform == 'win32'\nFoo_Bar==1.2.3\n"
    )
    pins, markered = script.load_pins(reqs)
    assert pins == {
        "httpx": "0.28.1",
        "colorama": "0.4.6",
        "foo-bar": "1.2.3",
    }
    assert markered == {"colorama"}


# --- main(): end-to-end drift detection ---------------------------------


def _write(tmp_path, name, lines):
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n")
    return path


def _run_main(script, monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["check_image_matches_lock.py", *argv])
    return script.main()


def test_main_matching_sets_pass(script, tmp_path, monkeypatch, capsys):
    lock = _write(tmp_path, "lock.txt", ["httpx==0.28.1", "pyjwt==2.13.0"])
    image = _write(tmp_path, "image.txt", ["httpx==0.28.1", "pyjwt==2.13.0"])

    exit_code = _run_main(script, monkeypatch, [str(lock), str(image)])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "OK: image matches uv.lock (2 pinned packages checked)" in out


def test_main_normalizes_names_across_sides(script, tmp_path, monkeypatch, capsys):
    # Lock pins the dotted spelling, image reports the hyphenated one (a real
    # pip-vs-uv rendering difference cited in the script's own docstring).
    lock = _write(tmp_path, "lock.txt", ["pdfminer.six==20251230"])
    image = _write(tmp_path, "image.txt", ["pdfminer-six==20251230"])

    exit_code = _run_main(script, monkeypatch, [str(lock), str(image)])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "OK" in out


def test_main_version_mismatch_fails_with_exact_message(script, tmp_path, monkeypatch, capsys):
    lock = _write(tmp_path, "lock.txt", ["httpx==0.28.1"])
    image = _write(tmp_path, "image.txt", ["httpx==0.27.0"])

    exit_code = _run_main(script, monkeypatch, [str(lock), str(image)])

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "VERSION MISMATCH (image installs a different version than uv.lock pins):" in out
    assert "  httpx: lock=0.28.1 image=0.27.0" in out


def test_main_missing_from_image_fails_with_exact_message(script, tmp_path, monkeypatch, capsys):
    lock = _write(tmp_path, "lock.txt", ["httpx==0.28.1", "pyjwt==2.13.0"])
    image = _write(tmp_path, "image.txt", ["httpx==0.28.1"])

    exit_code = _run_main(script, monkeypatch, [str(lock), str(image)])

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "MISSING FROM IMAGE (lock pins it, image did not install it):" in out
    assert "  pyjwt==2.13.0" in out


def test_main_unexpected_in_image_fails_with_exact_message(script, tmp_path, monkeypatch, capsys):
    lock = _write(tmp_path, "lock.txt", ["httpx==0.28.1"])
    image = _write(tmp_path, "image.txt", ["httpx==0.28.1", "requests==2.32.0"])

    exit_code = _run_main(script, monkeypatch, [str(lock), str(image)])

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "UNEXPECTED IN IMAGE (not in the lock export, not in --allow):" in out
    assert "  requests==2.32.0" in out


def test_main_allow_tolerates_editable_and_pip_packages(script, tmp_path, monkeypatch, capsys):
    lock = _write(tmp_path, "lock.txt", ["httpx==0.28.1"])
    image = _write(
        tmp_path,
        "image.txt",
        ["httpx==0.28.1", "herd-acl==0.3.0.dev0", "herd-common==0.3.0.dev0", "pip==24.0"],
    )

    exit_code = _run_main(
        script,
        monkeypatch,
        [
            str(lock),
            str(image),
            "--allow",
            "herd-acl",
            "--allow",
            "herd-common",
            "--allow",
            "pip",
        ],
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "OK" in out


def test_main_allow_normalizes_name_before_matching(script, tmp_path, monkeypatch, capsys):
    # --allow is matched through the same PEP 503 normalization as everything
    # else, so an underscored --allow value still tolerates a hyphenated
    # image-only package name.
    lock = _write(tmp_path, "lock.txt", ["httpx==0.28.1"])
    image = _write(tmp_path, "image.txt", ["httpx==0.28.1", "herd-acl==0.3.0.dev0"])

    exit_code = _run_main(script, monkeypatch, [str(lock), str(image), "--allow", "herd_acl"])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "OK" in out


def test_main_marker_excluded_package_is_not_expected_in_image(
    script, tmp_path, monkeypatch, capsys
):
    # colorama is win32-only in the lock export; a Linux image legitimately
    # lacks it, and that must not be reported as MISSING FROM IMAGE.
    lock = _write(
        tmp_path,
        "lock.txt",
        ["httpx==0.28.1", "colorama==0.4.6 ; sys_platform == 'win32'"],
    )
    image = _write(tmp_path, "image.txt", ["httpx==0.28.1"])

    exit_code = _run_main(script, monkeypatch, [str(lock), str(image)])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "OK: image matches uv.lock (2 pinned packages checked)" in out
    assert "colorama" not in out


def test_main_marker_free_lock_only_package_still_fails(script, tmp_path, monkeypatch, capsys):
    # Without a marker, a lock-only package is unexplained drift, not an
    # expected platform exclusion, and must still fail.
    lock = _write(tmp_path, "lock.txt", ["httpx==0.28.1", "pyjwt==2.13.0"])
    image = _write(tmp_path, "image.txt", ["httpx==0.28.1"])

    exit_code = _run_main(script, monkeypatch, [str(lock), str(image)])

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "MISSING FROM IMAGE (lock pins it, image did not install it):" in out
    assert "  pyjwt==2.13.0" in out


def test_main_reports_multiple_drift_categories_together(script, tmp_path, monkeypatch, capsys):
    lock = _write(tmp_path, "lock.txt", ["httpx==0.28.1", "pyjwt==2.13.0"])
    image = _write(tmp_path, "image.txt", ["httpx==0.27.0", "requests==2.32.0"])

    exit_code = _run_main(script, monkeypatch, [str(lock), str(image)])

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "VERSION MISMATCH" in out
    assert "MISSING FROM IMAGE" in out
    assert "UNEXPECTED IN IMAGE" in out
